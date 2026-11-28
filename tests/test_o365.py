"""Acceptance tests of the Office 365 calendar source (ADR 0006).

The gate reads the work calendar from Microsoft Graph with `msal` for the sign-in, here both
served by the TLS stand-in of `tests/o365_standin.py`. The credentials file is made once by
the operator's own `glovebox o365 login` against the stand-in, and copied for each test. The
gate's clock is pinned to `NOW` (2026-01-10T12:00Z), so the look-ahead ends 2026-04-10T12:00Z.
"""

import json
import os
import stat
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.client import (
    ACCOUNTS_TOOL,
    CALENDAR_TOOLS,
    EVENTS_TOOL,
    OVERLAPS_TOOL,
    agent_client,
)
from tests.conftest import GateStarter, RunningGate, operator
from tests.hostile import CORPUS, assert_no_corpus, hostile_graph_events, observe_gate
from tests.o365_standin import (
    CLIENT_ID,
    OIDC_SCOPES,
    TENANT,
    GraphStandIn,
    make_certificate,
)
from tests.test_calendar import calendar, event, events_of, spans_of, windows_of
from tests.test_gate import storage_files, write_fixture
from tests.test_queries import call

KIND = "o365_calendar"
DAY = {"range_from": "2026-01-12T08:00Z", "range_to": "2026-01-12T17:00Z"}


@pytest.fixture(scope="module")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return make_certificate(tmp_path_factory.mktemp("tls"))[0]


@pytest.fixture(scope="module")
def stand_in(certificate: Path) -> Iterator[GraphStandIn]:
    key = certificate.with_name("standin-key.pem")
    server = GraphStandIn(certificate, key)
    yield server
    server.close()


@pytest.fixture
def graph(stand_in: GraphStandIn) -> GraphStandIn:
    stand_in.reset()
    return stand_in


def trust(certificate: Path) -> dict[str, str]:
    """What makes the gate, `msal` and the operator's command trust the stand-in's certificate."""
    return {"SSL_CERT_FILE": str(certificate), "REQUESTS_CA_BUNDLE": str(certificate)}


def sign_in(graph: GraphStandIn, certificate: Path, target: Path, **options: str) -> Any:
    """The operator's own sign-in against the stand-in."""
    arguments = {"client-id": CLIENT_ID, "tenant-id": TENANT, "authority": graph.authority}
    arguments.update({name.replace("_", "-"): value for name, value in options.items()})
    flags = [part for name, value in arguments.items() for part in (f"--{name}", value)]
    return operator(
        target.parent / "login.db",
        "o365",
        "login",
        "--credentials",
        str(target),
        *flags,
        **trust(certificate),
    )


@pytest.fixture(scope="module")
def signed_in(
    stand_in: GraphStandIn, certificate: Path, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    stand_in.reset()
    target = tmp_path_factory.mktemp("login") / "work.json"
    result = sign_in(stand_in, certificate, target)
    assert result.returncode == 0, result.stderr
    return target


def graph_event(
    label: str, start: str, end: str, zone: str = "UTC", **fields: Any
) -> dict[str, Any]:
    """A Graph event with properties the gate must not read, and a time in `zone`."""
    return {
        "id": f"AAMk-{label}",
        "iCalUId": f"ical-{label}",
        "subject": f"Subject {label}",
        "bodyPreview": f"Preview {label}",
        "location": {"displayName": f"Room {label}"},
        "organizer": {"emailAddress": {"name": "Org", "address": "org@example.test"}},
        "start": {"dateTime": f"{start}.0000000", "timeZone": zone},
        "end": {"dateTime": f"{end}.0000000", "timeZone": zone},
        "isAllDay": False,
        "isCancelled": False,
        "showAs": "busy",
        "responseStatus": {"response": "accepted"},
        **fields,
    }


def week() -> list[object]:
    """Events on 12 and 13 January; the first two overlap and the last is free time."""
    return [
        graph_event("standup", "2026-01-12T09:00:00", "2026-01-12T09:30:00"),
        graph_event("review", "2026-01-12T09:15:00", "2026-01-12T10:00:00"),
        graph_event("lunch", "2026-01-12T12:00:00", "2026-01-12T13:00:00"),
        graph_event("gym", "2026-01-13T17:00:00", "2026-01-13T18:00:00", showAs="free"),
    ]


@contextmanager
def work_gate(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
    *,
    source: str | None = None,
    edit: Callable[[dict[str, Any]], None] | None = None,
    also: Mapping[str, Path] | None = None,
    db: str = "glovebox.db",
    now: str | None = None,
) -> Iterator[tuple[RunningGate, Path]]:
    """A gate with the work calendar account `work` (and `also`, fixture calendars by name),
    and its credentials file, a copy of the operator's sign-in that `edit` may change."""
    credentials = tmp_path / "work-login.json"
    document = json.loads(signed_in.read_text())
    if edit is not None:
        edit(document)
    credentials.write_text(json.dumps(document))
    credentials.chmod(0o600)
    location = source or graph.graph
    added = operator(
        tmp_path / db,
        "account",
        "add",
        "work",
        "--kind",
        KIND,
        "--source",
        location,
        "--credentials",
        str(credentials),
    )
    assert added.returncode == 0, added.stderr
    accounts: dict[str, Path | str] = {"work": location, **(also or {})}
    kinds = {name: "fixture_calendar" for name in also or {}} | {"work": KIND}
    with start_gate(
        db,
        accounts=accounts,
        kinds=kinds,
        tokens={
            "default": list(accounts),
            **{f"{name}-only": [name] for name in accounts},
        },
        extra_env=trust(certificate),
        **({} if now is None else {"now": now}),
    ) as gate:
        yield gate, credentials


def refresh_tokens(credentials: Path) -> set[str]:
    cache = json.loads(credentials.read_text())["token_cache"]
    return {entry["secret"] for entry in cache.get("RefreshToken", {}).values()}


def entry_of(result: dict[str, Any], kind: str = KIND) -> dict[str, Any]:
    entries: list[dict[str, Any]] = [e for e in result["accounts"] if e["source_kind"] == kind]
    (entry,) = entries
    return entry


# --- the calendar is served ---------------------------------------------------------------


@pytest.mark.anyio
async def test_the_work_calendar_serves_availability_overlaps_and_events(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
) -> None:
    graph.script.events = week()
    with work_gate(start_gate, tmp_path, graph, signed_in, certificate) as (gate, _):
        async with agent_client(gate) as client:
            windows = await windows_of(client, **DAY)
            overlaps = await call(client, OVERLAPS_TOOL)
            listed = await events_of(client)
            accounts = await call(client, ACCOUNTS_TOOL)

    assert windows == [
        ("2026-01-12T08:00Z", "2026-01-12T09:00Z", "free"),
        ("2026-01-12T09:00Z", "2026-01-12T10:00Z", "busy"),
        ("2026-01-12T10:00Z", "2026-01-12T12:00Z", "free"),
        ("2026-01-12T12:00Z", "2026-01-12T13:00Z", "busy"),
        ("2026-01-12T13:00Z", "2026-01-12T17:00Z", "free"),
    ]
    assert len(overlaps["events"]) == 2
    assert [(e["start"], e["busy_status"]) for e in listed] == [
        ("2026-01-12T09:00Z", "busy"),
        ("2026-01-12T09:15Z", "busy"),
        ("2026-01-12T12:00Z", "busy"),
        ("2026-01-13T17:00Z", "free"),
    ]
    for entry in listed:
        assert set(entry) == {
            "event_handle",
            "account_handle",
            "start",
            "end",
            "all_day",
            "busy_status",
        }
    (account,) = accounts["accounts"]
    assert account["source_kind"] == KIND
    assert account["read_only_enforcement"] == "provider"
    assert (account["status"], account["last_sync"]) == ("current", "2026-01-10T12:00Z")


@pytest.mark.anyio
async def test_the_gate_asks_only_for_the_read_permission_and_only_ever_reads(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
) -> None:
    graph.script.events = week()
    with work_gate(start_gate, tmp_path, graph, signed_in, certificate) as (gate, _):
        async with agent_client(gate) as client:
            for tool in CALENDAR_TOOLS:
                assert (await call(client, tool))["error"] == "none"

    refreshes = graph.token_requests()
    assert len(refreshes) == len(CALENDAR_TOOLS)
    for request in refreshes:
        scopes = set(request.form["scope"][0].split())
        assert scopes - OIDC_SCOPES == {"Calendars.Read"}
    # Everything sent to Graph is one read of the calendar view, over the look-ahead.
    requests = graph.graph_requests()
    assert {(r.method, r.path) for r in requests} == {("GET", "/v1.0/me/calendarView")}
    assert len(requests) == len(CALENDAR_TOOLS)
    for request in requests:
        assert request.query["startDateTime"] == ["2026-01-10T12:00:00Z"]
        assert request.query["endDateTime"] == ["2026-04-10T12:00:00Z"]
        assert request.headers["authorization"].startswith("Bearer access-")
    # Nothing else was asked of the stand-in but the sign-in's own configuration and token.
    assert {(r.method, r.path.rsplit("/", 1)[-1]) for r in graph.requests} <= {
        ("GET", "calendarView"),
        ("GET", "openid-configuration"),
        ("POST", "token"),
    }


@pytest.mark.anyio
async def test_recurring_all_day_and_answered_events_become_busy_and_free_time(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
) -> None:
    berlin = "Europe/Berlin"
    # Timed events come in UTC: 09:00 in Berlin is 08:00 UTC in winter.
    graph.script.events = [
        # Graph expands a series into its occurrences; each has an ID of its own.
        *[
            graph_event(
                f"series-{n}",
                f"2026-01-{day:02d}T08:00:00",
                f"2026-01-{day:02d}T09:00:00",
                type="occurrence",
                seriesMasterId="AAMk-series",
            )
            for n, day in enumerate((12, 19, 26))
        ],
        graph_event("cancelled", "2026-01-14T08:00:00", "2026-01-14T09:00:00")
        | {"isCancelled": True},
        graph_event("tentative", "2026-01-15T08:00:00", "2026-01-15T09:00:00")
        | {"showAs": "tentative"},
        graph_event("elsewhere", "2026-01-16T08:00:00", "2026-01-16T09:00:00")
        | {"showAs": "workingElsewhere"},
        graph_event("declined", "2026-01-17T08:00:00", "2026-01-17T09:00:00")
        | {"responseStatus": {"response": "declined"}},
        graph_event("not-answered", "2026-01-18T08:00:00", "2026-01-18T09:00:00")
        | {"responseStatus": {"response": "notResponded"}},
        graph_event("unknown", "2026-01-19T11:00:00", "2026-01-19T12:00:00")
        | {"showAs": "unknown"},
        # An all-day event in the UTC view is not read from it: its dates are the calendar's.
        graph_event("holiday", "2026-03-28T23:00:00", "2026-03-29T22:00:00", isAllDay=True),
    ]
    graph.script.events_in_zone[berlin] = [
        # All day on the day Berlin changes to summer time: 23 hours, between local midnights.
        graph_event(
            "holiday",
            "2026-03-29T00:00:00",
            "2026-03-30T00:00:00",
            berlin,
            isAllDay=True,
            showAs="oof",
        ),
        # A timed event in the zone's view is not read from it.
        graph_event("timed", "2026-01-21T09:00:00", "2026-01-21T10:00:00", berlin),
    ]

    def in_berlin(document: dict[str, Any]) -> None:
        document["timezone"] = berlin

    with work_gate(start_gate, tmp_path, graph, signed_in, certificate, edit=in_berlin) as (
        gate,
        _,
    ):
        async with agent_client(gate) as client:
            listed = await events_of(client, limit=50)

    # Timed events are asked for in UTC, all-day ones in the calendar's own zone.
    assert {r.headers["prefer"] for r in graph.graph_requests()} == {
        'outlook.timezone="UTC"',
        f'outlook.timezone="{berlin}"',
    }
    by_start = {e["start"]: e for e in listed}
    assert len(listed) == 10
    assert [s for s in by_start if s.startswith("2026-01-12") or s.startswith("2026-01-26")] == [
        "2026-01-12T08:00Z",
        "2026-01-26T08:00Z",
    ]
    assert "2026-01-21T08:00Z" not in by_start
    holiday = by_start["2026-03-28T23:00Z"]
    assert (holiday["end"], holiday["all_day"], holiday["busy_status"]) == (
        "2026-03-29T22:00Z",
        True,
        "busy",
    )
    statuses = {
        "2026-01-14T08:00Z": "free",  # cancelled
        "2026-01-15T08:00Z": "tentative",
        "2026-01-16T08:00Z": "free",  # working elsewhere
        "2026-01-17T08:00Z": "free",  # declined
        "2026-01-18T08:00Z": "tentative",  # not answered
        "2026-01-19T11:00Z": "unknown",
    }
    assert {start: by_start[start]["busy_status"] for start in statuses} == statuses


@pytest.mark.anyio
async def test_timed_events_keep_their_instant_across_the_end_of_summer_time(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
) -> None:
    """Berlin goes back from 03:00 CEST to 02:00 CET on 2026-10-25, so 02:00 to 03:00 local
    time happens twice. As UTC instants, nothing about the repeated hour is ambiguous."""
    berlin = "Europe/Berlin"
    graph.script.events = [
        graph_event("first-0230", "2026-10-25T00:30:00", "2026-10-25T00:45:00"),  # 02:30 CEST
        graph_event(
            "crossing", "2026-10-25T00:45:00", "2026-10-25T01:15:00"
        ),  # 02:45 CEST to 02:15 CET
        graph_event("second-0230", "2026-10-25T01:30:00", "2026-10-25T02:30:00"),  # 02:30 CET
    ]
    graph.script.events_in_zone[berlin] = [
        # The day is 25 hours long: local midnight to local midnight.
        graph_event("day", "2026-10-25T00:00:00", "2026-10-26T00:00:00", berlin, isAllDay=True),
    ]

    def in_berlin(document: dict[str, Any]) -> None:
        document["timezone"] = berlin

    with work_gate(
        start_gate,
        tmp_path,
        graph,
        signed_in,
        certificate,
        edit=in_berlin,
        now="2026-10-20T12:00:00Z",
    ) as (gate, _):
        async with agent_client(gate) as client:
            result = await call(client, EVENTS_TOOL, limit=50)

    assert result["error"] == "none"
    assert entry_of(result)["status"] == "current"
    assert [(e["start"], e["end"], e["all_day"]) for e in result["events"]] == [
        ("2026-10-24T22:00Z", "2026-10-25T23:00Z", True),
        ("2026-10-25T00:30Z", "2026-10-25T00:45Z", False),
        ("2026-10-25T00:45Z", "2026-10-25T01:15Z", False),
        ("2026-10-25T01:30Z", "2026-10-25T02:30Z", False),
    ]
    # Timed events are asked for in UTC, never in a zone that has a repeated hour.
    assert {r.headers["prefer"] for r in graph.graph_requests()} == {
        'outlook.timezone="UTC"',
        f'outlook.timezone="{berlin}"',
    }


@pytest.mark.anyio
async def test_a_timed_event_given_in_a_local_zone_is_skipped_not_guessed(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
) -> None:
    """If Graph ignored the request for UTC, a wall time in the zone could be either of the two
    instants in the repeated hour; the gate counts the event and does not pick one."""
    berlin = "Europe/Berlin"
    graph.script.events = [
        graph_event("ambiguous", "2026-10-25T02:30:00", "2026-10-25T02:45:00", berlin),
        graph_event("fine", "2026-10-25T08:00:00", "2026-10-25T09:00:00"),
    ]

    def in_berlin(document: dict[str, Any]) -> None:
        document["timezone"] = berlin

    with work_gate(
        start_gate,
        tmp_path,
        graph,
        signed_in,
        certificate,
        edit=in_berlin,
        now="2026-10-20T12:00:00Z",
    ) as (gate, _):
        async with agent_client(gate) as client:
            result = await call(client, EVENTS_TOOL)

    assert [e["start"] for e in result["events"]] == ["2026-10-25T08:00Z"]
    assert (entry_of(result)["status"], entry_of(result)["skipped_items"]) == ("incomplete", "1")


@pytest.mark.anyio
async def test_events_on_every_page_are_ingested(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
) -> None:
    graph.script.page_size = 40
    graph.script.events = [
        graph_event(
            f"n{n}",
            f"2026-01-{12 + n // 24:02d}T{n % 24:02d}:00:00",
            f"2026-01-{12 + n // 24:02d}T{n % 24:02d}:30:00",
        )
        for n in range(130)
    ]
    with work_gate(start_gate, tmp_path, graph, signed_in, certificate) as (gate, _):
        async with agent_client(gate) as client:
            seen: list[str] = []
            cursor: str | None = None
            while True:
                page = await call(
                    client, EVENTS_TOOL, limit=50, **({"cursor": cursor} if cursor else {})
                )
                seen += [e["event_handle"] for e in page["events"]]
                cursor = page["next_cursor"]
                if cursor is None:
                    break

    assert len(seen) == len(set(seen)) == 130
    assert page["accounts"][0]["status"] == "current"


@pytest.mark.anyio
async def test_the_work_calendar_is_served_together_with_a_private_one(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
) -> None:
    graph.script.events = [graph_event("w", "2026-01-12T09:30:00", "2026-01-12T10:30:00")]
    private = write_fixture(
        tmp_path / "private.json",
        calendar(event("p", "2026-01-12T09:00:00Z", "2026-01-12T10:00:00Z")),
    )
    with work_gate(
        start_gate, tmp_path, graph, signed_in, certificate, also={"private": private}
    ) as (gate, _):
        async with agent_client(gate) as client:
            both = await windows_of(client, **DAY)
            overlaps = await call(client, OVERLAPS_TOOL)
        async with agent_client(gate, gate.tokens["work-only"]) as client:
            work = await windows_of(client, **DAY)
            work_spans = await spans_of(client)

    assert both == [
        ("2026-01-12T08:00Z", "2026-01-12T09:00Z", "free"),
        ("2026-01-12T09:00Z", "2026-01-12T10:30Z", "busy"),
        ("2026-01-12T10:30Z", "2026-01-12T17:00Z", "free"),
    ]
    assert len(overlaps["events"]) == 2
    assert work_spans == ["2026-01-12T09:30Z/2026-01-12T10:30Z"]
    # A token that does not cover the private calendar sees nothing of it.
    assert work == [
        ("2026-01-12T08:00Z", "2026-01-12T09:30Z", "free"),
        ("2026-01-12T09:30Z", "2026-01-12T10:30Z", "busy"),
        ("2026-01-12T10:30Z", "2026-01-12T17:00Z", "free"),
    ]


# --- sign-in, consent and the permission -----------------------------------------------------


def test_the_operator_signs_in_with_the_read_permission_only(
    graph: GraphStandIn, certificate: Path, tmp_path: Path
) -> None:
    target = tmp_path / "work.json"

    result = sign_in(graph, certificate, target, timezone="Europe/Berlin")

    assert result.returncode == 0, result.stderr
    assert "USER-CODE" in result.stdout
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    document = json.loads(target.read_text())
    assert (document["client_id"], document["tenant_id"]) == (CLIENT_ID, TENANT)
    assert document["timezone"] == "Europe/Berlin"
    assert len(refresh_tokens(target)) == 1
    (device_code,) = [r for r in graph.requests if r.path.endswith("/devicecode")]
    assert set(device_code.form["scope"][0].split()) - OIDC_SCOPES == {"Calendars.Read"}
    # What the token endpoint is asked for names no other scope either, where it names any.
    for request in graph.token_requests():
        assert set(request.form.get("scope", ["Calendars.Read"])[0].split()) - OIDC_SCOPES == {
            "Calendars.Read"
        }


def test_a_sign_in_that_would_grant_a_write_permission_is_not_saved(
    graph: GraphStandIn, certificate: Path, tmp_path: Path
) -> None:
    graph.script.granted_scope = "Calendars.ReadWrite Calendars.Read"
    target = tmp_path / "work.json"

    result = sign_in(graph, certificate, target)

    assert result.returncode == 1
    assert "could write" in result.stderr
    assert not target.exists()


def test_a_sign_in_the_service_refuses_says_nothing_of_what_it_said(
    graph: GraphStandIn, certificate: Path, tmp_path: Path
) -> None:
    graph.script.token_status = 400
    graph.script.token_body = json.dumps(
        {"error": "access_denied", "error_description": CORPUS["injection"]}
    ).encode()
    target = tmp_path / "work.json"

    result = sign_in(graph, certificate, target)

    assert result.returncode == 1
    assert_no_corpus({"stdout": result.stdout, "stderr": result.stderr})
    assert not target.exists()


def test_the_account_needs_https_and_a_credentials_file_that_holds_a_sign_in(
    graph: GraphStandIn, signed_in: Path, tmp_path: Path
) -> None:
    db = tmp_path / "glovebox.db"
    good = tmp_path / "good.json"
    good.write_text(signed_in.read_text())
    good.chmod(0o600)
    garbage = tmp_path / "garbage.json"
    garbage.write_text('{"nothing": "here"}')
    garbage.chmod(0o600)
    wrong_zone = tmp_path / "zone.json"
    wrong_zone.write_text(
        json.dumps({**json.loads(signed_in.read_text()), "timezone": "Mars/Base"})
    )
    wrong_zone.chmod(0o600)
    open_file = tmp_path / "open.json"
    open_file.write_text(signed_in.read_text())
    open_file.chmod(0o644)

    def add(source: str, *credentials: Path) -> Any:
        flags = [part for path in credentials for part in ("--credentials", str(path))]
        return operator(db, "account", "add", "work", "--kind", KIND, "--source", source, *flags)

    refused = {
        "http": add("http://127.0.0.1:9/v1.0", good),
        "file": add(str(tmp_path / "calendar.json"), good),
        "no credentials": add(graph.graph),
        "garbage": add(graph.graph, garbage),
        "zone": add(graph.graph, wrong_zone),
        "open": add(graph.graph, open_file),
    }
    accepted = add(graph.graph, good)

    for name, result in refused.items():
        assert result.returncode == 1, name
        assert result.stderr.startswith("glovebox: "), name
    assert accepted.returncode == 0, accepted.stderr


@pytest.mark.anyio
async def test_a_missing_or_withdrawn_sign_in_blocks_only_the_work_account(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
) -> None:
    graph.script.events = week()
    private = write_fixture(
        tmp_path / "private.json",
        calendar(event("p", "2026-01-12T09:00:00Z", "2026-01-12T10:00:00Z")),
    )

    def nothing(document: dict[str, Any]) -> None:
        document["token_cache"] = {}

    with work_gate(
        start_gate, tmp_path, graph, signed_in, certificate, edit=nothing, also={"private": private}
    ) as (gate, _):
        async with agent_client(gate) as client:
            no_sign_in = await call(client, EVENTS_TOOL)
        async with agent_client(gate, gate.tokens["private-only"]) as client:
            private_events = await spans_of(client)

    assert no_sign_in["error"] == "source_unavailable"
    assert entry_of(no_sign_in)["status"] == "blocked"
    assert entry_of(no_sign_in, "fixture_calendar")["status"] == "current"
    assert private_events == ["2026-01-12T09:00Z/2026-01-12T10:00Z"]
    assert graph.graph_requests() == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "refusal",
    [
        {"error": "invalid_grant", "error_description": "AADSTS65001: consent withdrawn"},
        {"error": "consent_required"},
        {"error": "interaction_required"},
        {"error": "invalid_client"},
        {"error": "unauthorized_client"},
    ],
    ids=lambda refusal: refusal["error"],
)
async def test_consent_the_provider_withdraws_makes_the_account_blocked_until_it_is_back(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
    refusal: dict[str, str],
) -> None:
    graph.script.events = week()
    private = write_fixture(
        tmp_path / "private.json",
        calendar(event("p", "2026-01-12T09:00:00Z", "2026-01-12T10:00:00Z")),
    )
    with work_gate(
        start_gate, tmp_path, graph, signed_in, certificate, also={"private": private}
    ) as (gate, _):
        async with agent_client(gate) as client:
            before = await call(client, EVENTS_TOOL)
            graph.script.token_status, graph.script.token_body = 400, json.dumps(refusal).encode()
            withdrawn = await call(client, EVENTS_TOOL)
            graph.script.token_body = None
            graph.script.token_status = 200
            back = await call(client, EVENTS_TOOL)
        async with agent_client(gate, gate.tokens["private-only"]) as client:
            private_only = await call(client, EVENTS_TOOL)

    assert before["error"] == "none"
    assert withdrawn["error"] == "source_unavailable"
    assert entry_of(withdrawn)["status"] == "blocked"
    assert entry_of(withdrawn, "fixture_calendar")["status"] == "current"
    # The last sync time stays what it was.
    assert entry_of(withdrawn)["last_sync"] == entry_of(before)["last_sync"]
    assert back["error"] == "none" and entry_of(back)["status"] == "current"
    assert private_only["error"] == "none" and len(private_only["events"]) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("status", [401, 403])
async def test_graph_refusing_the_token_makes_the_account_blocked(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
    status: int,
) -> None:
    graph.script.graph_status = status
    with work_gate(start_gate, tmp_path, graph, signed_in, certificate) as (gate, _):
        async with agent_client(gate) as client:
            results = [await call(client, tool) for tool in CALENDAR_TOOLS]

    for result in results:
        assert result["error"] == "source_unavailable"
        assert entry_of(result)["status"] == "blocked"
        assert not result.get("events") and not result.get("windows")


@pytest.mark.anyio
async def test_a_token_that_could_write_is_refused_and_nothing_is_sent_to_graph(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
) -> None:
    graph.script.events = week()
    graph.script.granted_scope = "Calendars.ReadWrite"
    with work_gate(start_gate, tmp_path, graph, signed_in, certificate) as (gate, _):
        async with agent_client(gate) as client:
            result = await call(client, EVENTS_TOOL)

    assert result["error"] == "source_unavailable"
    assert entry_of(result)["status"] == "blocked"
    assert graph.graph_requests() == []


# --- outage, throttling and malformed answers -----------------------------------------------

# What each failure changes in the stand-in's script.
FAILURES: dict[str, dict[str, object]] = {
    "graph unavailable": {"graph_status": 503},
    "graph throttled": {"graph_status": 429},
    "graph error": {"graph_status": 500},
    "sign-in service down": {"token_status": 503, "token_body": b"<html>Unavailable</html>"},
    "sign-in throttled": {
        "token_status": 429,
        "token_body": b'{"error": "temporarily_unavailable"}',
    },
    "not json": {"graph_body": b"<html>not json"},
    "not an object": {"graph_body": b"[]"},
    "no value": {"graph_body": b'{"events": []}'},
    "value is not a list": {"graph_body": b'{"value": "none"}'},
    "next link is not text": {"graph_body": b'{"value": [], "@odata.nextLink": 7}'},
}


@pytest.mark.anyio
@pytest.mark.parametrize("failure", list(FAILURES))
async def test_an_outage_throttling_or_malformed_answer_is_stale_never_an_empty_calendar(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
    failure: str,
) -> None:
    graph.script.events = week()
    with work_gate(start_gate, tmp_path, graph, signed_in, certificate) as (gate, _):
        async with agent_client(gate) as client:
            good = await call(client, EVENTS_TOOL)
            for name, value in FAILURES[failure].items():
                setattr(graph.script, name, value)
            results = [await call(client, tool) for tool in CALENDAR_TOOLS]
            graph.reset()
            graph.script.events = week()
            recovered = await call(client, EVENTS_TOOL)

    assert good["error"] == "none"
    for result in results:
        assert result["error"] == "source_unavailable"
        assert entry_of(result)["status"] == "stale"
        assert entry_of(result)["last_sync"] == "2026-01-10T12:00Z"
        assert not result.get("events") and not result.get("windows")
    assert recovered["error"] == "none"
    assert entry_of(recovered)["status"] == "current"
    assert len(recovered["events"]) == 4


@pytest.mark.anyio
async def test_a_graph_that_cannot_be_reached_is_an_outage(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
) -> None:
    gone = GraphStandIn(certificate, certificate.with_name("standin-key.pem"))
    source = gone.graph
    gone.close()
    with work_gate(start_gate, tmp_path, graph, signed_in, certificate, source=source) as (gate, _):
        async with agent_client(gate) as client:
            result = await call(client, EVENTS_TOOL)

    assert result["error"] == "source_unavailable"
    assert (entry_of(result)["status"], entry_of(result)["last_sync"]) == ("stale", None)


@pytest.mark.anyio
async def test_events_that_cannot_be_understood_are_skipped_and_counted(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
) -> None:
    good = graph_event("good", "2026-01-12T09:00:00", "2026-01-12T10:00:00")
    graph.script.events = [
        good,
        graph_event("backwards", "2026-01-12T11:00:00", "2026-01-12T10:00:00"),
        graph_event("zero", "2026-01-12T11:00:00", "2026-01-12T11:00:30"),
        graph_event(
            "other zone", "2026-01-12T11:00:00", "2026-01-12T12:00:00", "Pacific Standard Time"
        ),
        graph_event("not a time", "yesterday", "2026-01-12T12:00:00"),
        graph_event("offset", "2026-01-12T11:00:00+01:00", "2026-01-12T12:00:00+01:00"),
        graph_event("bad flag", "2026-01-12T11:00:00", "2026-01-12T12:00:00") | {"isAllDay": "yes"},
        graph_event("backwards day", "2026-01-12T00:00:00", "2026-01-12T00:00:00")
        | {"isAllDay": True},
        {"subject": "no id", "start": good["start"], "end": good["end"]},
        "not an object",
    ]
    with work_gate(start_gate, tmp_path, graph, signed_in, certificate) as (gate, _):
        async with agent_client(gate) as client:
            result = await call(client, EVENTS_TOOL)

    assert [e["start"] for e in result["events"]] == ["2026-01-12T09:00Z"]
    assert (entry_of(result)["status"], entry_of(result)["skipped_items"]) == ("incomplete", "9")


@pytest.mark.anyio
async def test_events_outside_the_look_ahead_are_not_served(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
) -> None:
    graph.script.events = [
        graph_event("ended", "2026-01-10T10:00:00", "2026-01-10T11:00:00"),
        graph_event("cut", "2026-04-10T11:00:00", "2026-04-10T14:00:00"),
        graph_event("beyond", "2026-04-10T12:00:00", "2026-04-10T13:00:00"),
    ]
    with work_gate(start_gate, tmp_path, graph, signed_in, certificate) as (gate, _):
        async with agent_client(gate) as client:
            spans = await spans_of(client)

    assert spans == ["2026-04-10T11:00Z/2026-04-10T12:00Z"]


@pytest.mark.anyio
async def test_a_next_page_link_to_another_host_is_not_followed_with_the_token(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
) -> None:
    elsewhere = GraphStandIn(certificate, certificate.with_name("standin-key.pem"))
    try:
        graph.script.page_size = 2
        graph.script.events = week()
        graph.script.next_link_base = elsewhere.graph
        with work_gate(start_gate, tmp_path, graph, signed_in, certificate) as (gate, _):
            async with agent_client(gate) as client:
                result = await call(client, EVENTS_TOOL)
        received = list(elsewhere.requests)
    finally:
        elsewhere.close()

    assert result["error"] == "source_unavailable"
    assert entry_of(result)["status"] == "stale"
    assert received == []


# --- the credentials file ----------------------------------------------------------------


@pytest.mark.anyio
async def test_a_refreshed_sign_in_is_written_back_and_used_by_the_next_sync(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
) -> None:
    graph.script.events = week()
    with work_gate(start_gate, tmp_path, graph, signed_in, certificate) as (gate, credentials):
        first_cache = refresh_tokens(credentials)
        async with agent_client(gate) as client:
            await call(client, EVENTS_TOOL)
            after_first = refresh_tokens(credentials)
            await call(client, EVENTS_TOOL)
        mode = stat.S_IMODE(credentials.stat().st_mode)
        document = json.loads(credentials.read_text())

    sent = [request.form["refresh_token"][0] for request in graph.token_requests()]
    assert sent[0] in first_cache
    assert after_first != first_cache and sent[1] in after_first
    assert mode == 0o600
    assert (document["client_id"], document["tenant_id"]) == (CLIENT_ID, TENANT)
    assert [name for name in os.listdir(tmp_path) if name.startswith(".o365-")] == []


# --- the hostile corpus ------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_corpus_string_reaches_any_channel_from_hostile_events_or_error_text(
    start_gate: GateStarter,
    tmp_path: Path,
    graph: GraphStandIn,
    signed_in: Path,
    certificate: Path,
) -> None:
    graph.script.events = hostile_graph_events()
    text = {name: CORPUS[name] for name in ("injection", "role_tag", "marker", "json_break")}
    errors = {
        "graph 500": ("graph", 500, {"error": {"code": CORPUS["marker"], "message": text}}),
        "graph 429": ("graph", 429, {"error": {"message": CORPUS["tool_call"]}}),
        "graph 401": ("graph", 401, {"error": {"message": CORPUS["role_tag"]}}),
        "graph html": ("graph", 200, CORPUS["injection"]),
        "token refused": (
            "token",
            400,
            {"error": "invalid_grant", "error_description": CORPUS["injection"], "x": text},
        ),
        "token unknown error": (
            "token",
            400,
            {"error": CORPUS["tool_call"], "error_description": CORPUS["markdown"]},
        ),
        "token html": ("token", 500, CORPUS["newlines"]),
        # Every corpus string at once, in the places an error body can hold text.
        "graph every string": (
            "graph",
            500,
            {"error": {"code": CORPUS["marker"], "message": CORPUS, "innerError": [CORPUS]}},
        ),
        "token every string": (
            "token",
            400,
            {"error": "invalid_grant", "error_description": CORPUS["sql"], "trace": CORPUS},
        ),
        "token scope": (
            "token",
            200,
            {"scope": CORPUS["injection"], "access_token": CORPUS["long"]},
        ),
        "next link": (
            "graph",
            200,
            {"value": [], "@odata.nextLink": f"https://evil.test/{CORPUS['markdown']}"},
        ),
    }
    with work_gate(start_gate, tmp_path, graph, signed_in, certificate) as (gate, _):
        channels = await observe_gate(gate)
        async with agent_client(gate) as client:
            events = await events_of(client, limit=50)
            for name, (service, status, body) in errors.items():
                raw = (body if isinstance(body, str) else json.dumps(body)).encode()
                setattr(graph.script, f"{service}_status", status)
                setattr(graph.script, f"{service}_body", raw)
                for tool in CALENDAR_TOOLS:
                    result = await client.call_tool(tool, {})
                    channels[f"{name} {tool}"] = result.model_dump_json(by_alias=True)
                graph.reset()
                graph.script.events = hostile_graph_events()
        channels["log-after-errors"] = gate.log.read_text(errors="replace")

    assert len(events) >= len(CORPUS)
    assert {"call-get_availability", "call-list_events", "call-list_overlaps"} <= set(channels)
    assert_no_corpus(channels)
    assert_no_corpus(storage_files(gate.db))
    for entry in events:
        assert entry["busy_status"] in {"busy", "tentative", "free", "unknown"}
