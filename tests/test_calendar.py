"""Acceptance tests of calendar availability served through the calendar tools (ADR 0006).

Everything goes through MCP tool calls from a real client. A calendar account is one of
source kind `fixture_calendar`: a document of events. The gate's clock is pinned to `NOW`
(2026-01-10T12:00Z), so the 90-day look-ahead ends at 2026-04-10T12:00Z and the Europe/Berlin
change to summer time (2026-03-29) and the New York one (2026-03-08) fall inside it. Tests
that read the database say so: what is stored is not visible through a tool call.
"""

import json
import re
import sqlite3
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from glovebox.formats import MAX_EVENTS
from tests.client import (
    ACCOUNTS_TOOL,
    AVAILABILITY_TOOL,
    CALENDAR_TOOLS,
    COUNT_TOOL,
    EVENTS_TOOL,
    LIST_TOOL,
    OVERLAPS_TOOL,
    agent_client,
)
from tests.conftest import GateStarter, RunningGate, operator
from tests.hostile import CORPUS, assert_no_corpus, hostile_calendar_fixture, observe_gate
from tests.schema import closed_schema_problems
from tests.test_gate import MINUTE, storage_files, write_fixture
from tests.test_queries import call, mailbox
from tests.test_standins import CAST, run_seed, serve

HANDLE = re.compile(r"^[a-z2-7]{24}$")
CALENDAR = "fixture_calendar"
EVENT_FIELDS = {"event_handle", "account_handle", "start", "end", "all_day", "busy_status"}
BUSY_STATUSES = {"busy", "tentative", "free", "unknown"}
LOOK_AHEAD_END = "2026-04-10T12:00Z"
UTC_FORMAT = "%Y-%m-%dT%H:%M:00Z"


def event(label: str, start: str, end: str | None, **fields: object) -> dict[str, object]:
    return {
        "id": label,
        "title": f"Title {label}",
        "description": f"Description {label}",
        "location": f"Location {label}",
        "organizer": f"organizer-{label}@example.test",
        "start": start,
        "end": end,
        **fields,
    }


def calendar(*events: object, timezone: str | None = None) -> dict[str, object]:
    document: dict[str, object] = {"account": "alice@example.test", "events": list(events)}
    if timezone is not None:
        document["timezone"] = timezone
    return document


def week_calendar() -> dict[str, object]:
    """Four events on 12 and 13 January; the first two overlap and the last is free time."""
    return calendar(
        event("standup", "2026-01-12T09:00:00Z", "2026-01-12T09:30:00Z"),
        event("review", "2026-01-12T09:15:00Z", "2026-01-12T10:00:00Z"),
        event("lunch", "2026-01-12T12:00:00Z", "2026-01-12T13:00:00Z"),
        event("gym", "2026-01-13T17:00:00Z", "2026-01-13T18:00:00Z", transparency="transparent"),
    )


def timed(label: str, first: datetime, begins: timedelta, lasts: timedelta) -> dict[str, object]:
    """An event that begins `begins` after `first` and lasts `lasts`, in UTC."""
    start = first + begins
    return event(label, start.strftime(UTC_FORMAT), (start + lasts).strftime(UTC_FORMAT))


def publish(
    start_gate: GateStarter,
    tmp_path: Path,
    document: object,
    **options: Any,
) -> AbstractContextManager[RunningGate]:
    """Start a gate whose one account is a calendar serving `document`."""
    source = tmp_path / "calendar.json"
    source.write_text(json.dumps(document))
    return start_gate(
        accounts={"cal": source}, kinds={"cal": CALENDAR}, tokens={"default": ["cal"]}, **options
    )


async def events_of(client: Client, **arguments: object) -> list[dict[str, Any]]:
    result = await call(client, EVENTS_TOOL, **arguments)
    assert result["error"] == "none", result
    events: list[dict[str, Any]] = result["events"]
    return events


async def windows_of(client: Client, **arguments: object) -> list[tuple[str, str, str]]:
    result = await call(client, AVAILABILITY_TOOL, **arguments)
    assert result["error"] == "none", result
    return [(w["start"], w["end"], w["state"]) for w in result["windows"]]


async def spans_of(client: Client, tool: str = EVENTS_TOOL, **arguments: object) -> list[str]:
    """The start and end of each event the tool returns, as `start/end`."""
    result = await call(client, tool, **arguments)
    assert result["error"] == "none", result
    return [f"{e['start']}/{e['end']}" for e in result["events"]]


# --- listing events ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_upcoming_events_carry_only_handles_times_and_a_busy_status(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, week_calendar()) as gate:
        async with agent_client(gate) as client:
            result = await call(client, EVENTS_TOOL)

    assert set(result) == {"error", "accounts", "events", "next_cursor"}
    assert result["next_cursor"] is None
    (sync,) = result["accounts"]
    assert (sync["source_kind"], sync["status"], sync["skipped_items"]) == (
        CALENDAR,
        "current",
        "0",
    )
    events = result["events"]
    assert [(e["start"], e["end"]) for e in events] == [
        ("2026-01-12T09:00Z", "2026-01-12T09:30Z"),
        ("2026-01-12T09:15Z", "2026-01-12T10:00Z"),
        ("2026-01-12T12:00Z", "2026-01-12T13:00Z"),
        ("2026-01-13T17:00Z", "2026-01-13T18:00Z"),
    ]
    for entry in events:
        assert set(entry) == EVENT_FIELDS
        assert HANDLE.match(entry["event_handle"])
        assert entry["account_handle"] == sync["account_handle"]
        assert MINUTE.match(entry["start"]) and MINUTE.match(entry["end"])
        assert entry["all_day"] is False
    assert [e["busy_status"] for e in events] == ["busy", "busy", "busy", "free"]
    assert len({e["event_handle"] for e in events}) == 4


@pytest.mark.anyio
async def test_event_times_are_minute_precision_in_utc_whatever_the_source_offset(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    document = calendar(
        event("a", "2026-01-12T10:00:59+01:00", "2026-01-12T10:30:30+01:00"),
        event("b", "2026-01-12T09:00:00-05:00", "2026-01-12T10:00:00-05:00"),
    )
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            spans = await spans_of(client)

    assert spans == [
        "2026-01-12T09:00Z/2026-01-12T09:30Z",
        "2026-01-12T14:00Z/2026-01-12T15:00Z",
    ]


@pytest.mark.anyio
async def test_events_sort_by_start_in_the_two_fixed_orders_and_filter_on_permitted_fields(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    document = calendar(
        event("one", "2026-01-12T09:00:00Z", "2026-01-12T10:00:00Z"),
        event("two", "2026-01-12T11:00:00Z", "2026-01-12T12:00:00Z", status="tentative"),
        event("three", "2026-01-14", "2026-01-15", all_day=True),
        event("four", "2026-01-13T11:00:00Z", "2026-01-13T12:00:00Z", transparency="transparent"),
    )
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            ascending = await events_of(client)
            descending = await events_of(client, sort="start_desc")
            tentative = await events_of(client, busy_status="tentative")
            all_day = await events_of(client, all_day=True)
            timed = await events_of(client, all_day=False)
            ranged = await events_of(
                client, range_from="2026-01-12T09:30Z", range_to="2026-01-12T11:00Z"
            )
            same_account = await events_of(client, account_handle=ascending[0]["account_handle"])
            other_account = await events_of(client, account_handle="a" * 24)

    assert [e["start"] for e in ascending] == [
        "2026-01-12T09:00Z",
        "2026-01-12T11:00Z",
        "2026-01-13T11:00Z",
        "2026-01-14T00:00Z",
    ]
    assert descending == ascending[::-1]
    assert [e["busy_status"] for e in ascending] == ["busy", "tentative", "free", "busy"]
    assert [e["start"] for e in tentative] == ["2026-01-12T11:00Z"]
    assert [(e["start"], e["end"]) for e in all_day] == [("2026-01-14T00:00Z", "2026-01-15T00:00Z")]
    assert len(timed) == 3
    # A range selects the events that overlap it; one that only touches it is not selected.
    assert [e["start"] for e in ranged] == ["2026-01-12T09:00Z"]
    assert same_account == ascending
    assert other_account == []


# --- busy and free windows --------------------------------------------------------------


@pytest.mark.anyio
async def test_busy_and_free_windows_partition_the_range_and_merge_overlaps(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, week_calendar()) as gate:
        async with agent_client(gate) as client:
            windows = await windows_of(
                client, range_from="2026-01-12T08:00Z", range_to="2026-01-12T14:00Z"
            )
            # A window is cut to the range.
            clipped = await windows_of(
                client, range_from="2026-01-12T09:20Z", range_to="2026-01-12T09:40Z"
            )
            # A free event does not make the time busy.
            unbooked = await windows_of(
                client, range_from="2026-01-13T16:00Z", range_to="2026-01-13T19:00Z"
            )

    assert windows == [
        ("2026-01-12T08:00Z", "2026-01-12T09:00Z", "free"),
        ("2026-01-12T09:00Z", "2026-01-12T10:00Z", "busy"),
        ("2026-01-12T10:00Z", "2026-01-12T12:00Z", "free"),
        ("2026-01-12T12:00Z", "2026-01-12T13:00Z", "busy"),
        ("2026-01-12T13:00Z", "2026-01-12T14:00Z", "free"),
    ]
    assert clipped == [("2026-01-12T09:20Z", "2026-01-12T09:40Z", "busy")]
    assert unbooked == [("2026-01-13T16:00Z", "2026-01-13T19:00Z", "free")]


@pytest.mark.anyio
async def test_back_to_back_events_make_one_busy_window(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    document = calendar(
        event("a", "2026-01-12T09:00:00Z", "2026-01-12T10:00:00Z"),
        event("b", "2026-01-12T10:00:00Z", "2026-01-12T11:00:00Z"),
    )
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            windows = await windows_of(
                client, range_from="2026-01-12T08:00Z", range_to="2026-01-12T12:00Z"
            )

    assert windows == [
        ("2026-01-12T08:00Z", "2026-01-12T09:00Z", "free"),
        ("2026-01-12T09:00Z", "2026-01-12T11:00Z", "busy"),
        ("2026-01-12T11:00Z", "2026-01-12T12:00Z", "free"),
    ]


@pytest.mark.anyio
async def test_availability_without_a_range_covers_the_whole_look_ahead(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, week_calendar()) as gate:
        async with agent_client(gate) as client:
            result = await call(client, AVAILABILITY_TOOL)

    assert set(result) == {"error", "accounts", "windows", "next_cursor"}
    assert result["next_cursor"] is None
    assert [(w["start"], w["end"], w["state"]) for w in result["windows"]] == [
        ("2026-01-10T12:00Z", "2026-01-12T09:00Z", "free"),
        ("2026-01-12T09:00Z", "2026-01-12T10:00Z", "busy"),
        ("2026-01-12T10:00Z", "2026-01-12T12:00Z", "free"),
        ("2026-01-12T12:00Z", "2026-01-12T13:00Z", "busy"),
        ("2026-01-12T13:00Z", LOOK_AHEAD_END, "free"),
    ]


@pytest.mark.anyio
async def test_a_range_outside_the_look_ahead_is_cut_to_it(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, week_calendar()) as gate:
        async with agent_client(gate) as client:
            both_sides = await windows_of(
                client, range_from="2025-01-01T00:00Z", range_to="2030-01-01T00:00Z"
            )
            past = await windows_of(
                client, range_from="2025-01-01T00:00Z", range_to="2026-01-09T00:00Z"
            )
            beyond = await windows_of(
                client, range_from="2026-05-01T00:00Z", range_to="2026-06-01T00:00Z"
            )
            reversed_range = await windows_of(
                client, range_from="2026-01-13T00:00Z", range_to="2026-01-12T00:00Z"
            )

    assert (both_sides[0][0], both_sides[-1][1]) == ("2026-01-10T12:00Z", LOOK_AHEAD_END)
    assert past == beyond == reversed_range == []


@pytest.mark.anyio
async def test_availability_pages_are_fifty_windows_with_a_cursor_for_the_rest(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    # Sixty one-hour events, two hours apart: a busy and a free window each, 120 in all.
    first_day = datetime(2026, 1, 12)
    events = [
        timed(f"e{index}", first_day, timedelta(hours=index * 2), timedelta(hours=1))
        for index in range(60)
    ]
    window = {"range_from": "2026-01-12T00:00Z", "range_to": "2026-01-17T00:00Z"}
    with publish(start_gate, tmp_path, calendar(*events)) as gate:
        async with agent_client(gate) as client:
            first = await call(client, AVAILABILITY_TOOL, **window)
            second = await call(client, AVAILABILITY_TOOL, cursor=first["next_cursor"], **window)
            third = await call(client, AVAILABILITY_TOOL, cursor=second["next_cursor"], **window)
            small = await call(client, AVAILABILITY_TOOL, limit=2, **window)
            other_range = await call(
                client,
                AVAILABILITY_TOOL,
                cursor=first["next_cursor"],
                range_from="2026-01-12T00:00Z",
                range_to="2026-01-18T00:00Z",
            )

    pages = [first["windows"], second["windows"], third["windows"]]
    assert [len(page) for page in pages] == [50, 50, 20]
    assert third["next_cursor"] is None
    starts = [w["start"] for page in pages for w in page]
    assert starts == sorted(set(starts))
    assert [w["state"] for w in first["windows"][:4]] == ["busy", "free", "busy", "free"]
    assert len(small["windows"]) == 2
    assert other_range["error"] == "invalid_cursor"


# --- overlaps ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_overlapping_events_are_returned_with_their_handles_and_times(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, week_calendar()) as gate:
        async with agent_client(gate) as client:
            result = await call(client, OVERLAPS_TOOL)
            everything = await events_of(client)
            narrow = await spans_of(
                client, OVERLAPS_TOOL, range_from="2026-01-12T11:00Z", range_to="2026-01-12T14:00Z"
            )

    assert set(result) == {"error", "accounts", "events", "next_cursor"}
    assert [(e["start"], e["end"]) for e in result["events"]] == [
        ("2026-01-12T09:00Z", "2026-01-12T09:30Z"),
        ("2026-01-12T09:15Z", "2026-01-12T10:00Z"),
    ]
    for entry in result["events"]:
        assert set(entry) == EVENT_FIELDS
        assert entry in everything
    # Lunch overlaps nothing, and the free gym event overlaps nothing that blocks time.
    assert narrow == []


@pytest.mark.anyio
async def test_events_that_only_touch_or_are_free_are_not_overlaps(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    document = calendar(
        event("a", "2026-01-12T09:00:00Z", "2026-01-12T10:00:00Z"),
        event("touching", "2026-01-12T10:00:00Z", "2026-01-12T11:00:00Z"),
        event("free", "2026-01-12T09:30:00Z", "2026-01-12T10:30:00Z", transparency="transparent"),
        event("declined", "2026-01-12T09:30:00Z", "2026-01-12T10:30:00Z", participation="declined"),
        event("cancelled", "2026-01-12T09:30:00Z", "2026-01-12T10:30:00Z", status="cancelled"),
        event("tentative", "2026-01-12T10:30:00Z", "2026-01-12T11:30:00Z", status="tentative"),
    )
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            overlaps = await spans_of(client, OVERLAPS_TOOL)

    assert overlaps == [
        "2026-01-12T10:00Z/2026-01-12T11:00Z",
        "2026-01-12T10:30Z/2026-01-12T11:30Z",
    ]


@pytest.mark.anyio
async def test_availability_and_overlaps_span_the_calendar_accounts_a_token_covers(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    private = write_fixture(
        tmp_path / "private.json",
        calendar(event("p", "2026-01-12T09:00:00Z", "2026-01-12T10:00:00Z")),
    )
    work = write_fixture(
        tmp_path / "work.json",
        calendar(
            event("w1", "2026-01-12T09:30:00Z", "2026-01-12T10:30:00Z"),
            event("w2", "2026-01-12T15:00:00Z", "2026-01-12T16:00:00Z"),
        ),
    )
    day = {"range_from": "2026-01-12T08:00Z", "range_to": "2026-01-12T17:00Z"}
    with start_gate(
        accounts={"private": private, "work": work},
        kinds={"private": CALENDAR, "work": CALENDAR},
        tokens={"default": ["private", "work"], "private-only": ["private"]},
    ) as gate:
        async with agent_client(gate) as client:
            overlaps = await call(client, OVERLAPS_TOOL)
            windows = await windows_of(client, **day)
            listed = await events_of(client)
        async with agent_client(gate, gate.tokens["private-only"]) as client:
            only_private = await windows_of(client, **day)
            private_overlaps = await spans_of(client, OVERLAPS_TOOL)

    assert len(overlaps["accounts"]) == 2
    assert len(overlaps["events"]) == 2
    assert len({e["account_handle"] for e in overlaps["events"]}) == 2
    assert windows == [
        ("2026-01-12T08:00Z", "2026-01-12T09:00Z", "free"),
        ("2026-01-12T09:00Z", "2026-01-12T10:30Z", "busy"),
        ("2026-01-12T10:30Z", "2026-01-12T15:00Z", "free"),
        ("2026-01-12T15:00Z", "2026-01-12T16:00Z", "busy"),
        ("2026-01-12T16:00Z", "2026-01-12T17:00Z", "free"),
    ]
    assert len(listed) == 3
    # A token that does not cover the work calendar sees nothing of it.
    assert only_private == [
        ("2026-01-12T08:00Z", "2026-01-12T09:00Z", "free"),
        ("2026-01-12T09:00Z", "2026-01-12T10:00Z", "busy"),
        ("2026-01-12T10:00Z", "2026-01-12T17:00Z", "free"),
    ]
    assert private_overlaps == []


@pytest.mark.anyio
async def test_overlaps_page_with_a_cursor(start_gate: GateStarter, tmp_path: Path) -> None:
    # Thirty days with two overlapping events each: sixty events that overlap another.
    first_day = datetime(2026, 2, 1, 9)
    events = []
    for day in range(30):
        begins = timedelta(days=day)
        events.append(timed(f"a{day}", first_day, begins, timedelta(hours=1)))
        events.append(
            timed(f"b{day}", first_day, begins + timedelta(minutes=30), timedelta(hours=1))
        )
    with publish(start_gate, tmp_path, calendar(*events)) as gate:
        async with agent_client(gate) as client:
            first = await call(client, OVERLAPS_TOOL)
            second = await call(client, OVERLAPS_TOOL, cursor=first["next_cursor"])

    assert len(first["events"]) == 50
    assert len(second["events"]) == 10
    assert second["next_cursor"] is None
    handles = [e["event_handle"] for e in first["events"] + second["events"]]
    assert len(set(handles)) == 60


# --- the look-ahead ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_events_beyond_the_look_ahead_are_neither_ingested_nor_served(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    document = calendar(
        event("near", "2026-04-10T09:00:00Z", "2026-04-10T10:00:00Z"),
        event("last-minute", "2026-04-10T11:59:00Z", "2026-04-10T13:00:00Z"),
        event("at-the-end", "2026-04-10T12:00:00Z", "2026-04-10T13:00:00Z"),
        event("later", "2026-07-01T09:00:00Z", "2026-07-01T10:00:00Z"),
        event("straddling", "2026-04-10T11:00:00Z", "2026-04-11T11:00:00Z"),
        event("past", "2026-01-09T09:00:00Z", "2026-01-09T10:00:00Z"),
        event("running", "2026-01-10T11:00:00Z", "2026-01-10T13:00:00Z"),
        event("ended", "2026-01-10T11:00:00Z", "2026-01-10T12:00:00Z"),
    )
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            spans = await spans_of(client)
            beyond = await spans_of(
                client, range_from="2026-04-10T12:00Z", range_to="2026-08-01T00:00Z"
            )
            windows = await windows_of(
                client, range_from="2026-04-10T09:00Z", range_to="2026-04-12T00:00Z"
            )
        # Below the seam on purpose: not ingested means not stored.
        with sqlite3.connect(gate.db) as database:
            stored = database.execute("select count(*) from events").fetchone()[0]

    # What runs past the end of the look-ahead is cut there; what starts at it is left out.
    assert spans == [
        "2026-01-10T11:00Z/2026-01-10T13:00Z",
        "2026-04-10T09:00Z/2026-04-10T10:00Z",
        "2026-04-10T11:00Z/2026-04-10T12:00Z",
        "2026-04-10T11:59Z/2026-04-10T12:00Z",
    ]
    assert beyond == []
    assert windows == [
        ("2026-04-10T09:00Z", "2026-04-10T10:00Z", "busy"),
        ("2026-04-10T10:00Z", "2026-04-10T11:00Z", "free"),
        ("2026-04-10T11:00Z", "2026-04-10T12:00Z", "busy"),
    ]
    assert stored == 4


# --- daylight saving, recurrence and all-day events -------------------------------------


@pytest.mark.anyio
async def test_a_weekly_event_keeps_its_wall_clock_time_across_the_change_to_summer_time(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    # Mondays at 09:00 in Berlin: 08:00Z in winter, 07:00Z after the change on 29 March.
    document = calendar(
        event(
            "weekly",
            "2026-03-09T09:00:00",
            "2026-03-09T10:00:00",
            timezone="Europe/Berlin",
            recurrence={"freq": "weekly", "count": 4},
        )
    )
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            spans = await spans_of(client)
            windows = await windows_of(
                client, range_from="2026-03-29T00:00Z", range_to="2026-03-31T00:00Z"
            )

    assert spans == [
        "2026-03-09T08:00Z/2026-03-09T09:00Z",
        "2026-03-16T08:00Z/2026-03-16T09:00Z",
        "2026-03-23T08:00Z/2026-03-23T09:00Z",
        "2026-03-30T07:00Z/2026-03-30T08:00Z",
    ]
    assert windows == [
        ("2026-03-29T00:00Z", "2026-03-30T07:00Z", "free"),
        ("2026-03-30T07:00Z", "2026-03-30T08:00Z", "busy"),
        ("2026-03-30T08:00Z", "2026-03-31T00:00Z", "free"),
    ]


@pytest.mark.anyio
async def test_a_daily_event_across_the_change_to_summer_time_in_new_york(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    # 08:00 in New York: 13:00Z before the change on 8 March, 12:00Z after it.
    document = calendar(
        event(
            "daily",
            "2026-03-07T08:00:00-05:00",
            "2026-03-07T08:30:00-05:00",
            timezone="America/New_York",
            recurrence={"freq": "daily", "count": 3},
        )
    )
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            spans = await spans_of(client)

    assert spans == [
        "2026-03-07T13:00Z/2026-03-07T13:30Z",
        "2026-03-08T12:00Z/2026-03-08T12:30Z",
        "2026-03-09T12:00Z/2026-03-09T12:30Z",
    ]


@pytest.mark.anyio
async def test_a_timed_event_over_the_change_keeps_its_exact_duration(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    # 01:00 to 04:00 local on the night of the change is two hours long.
    document = calendar(
        event("night", "2026-03-29T01:00:00", "2026-03-29T04:00:00", timezone="Europe/Berlin"),
        event("morning", "2026-03-28T09:00:00", "2026-03-28T10:00:00", timezone="Europe/Berlin"),
    )
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            spans = await spans_of(client)

    assert spans == ["2026-03-28T08:00Z/2026-03-28T09:00Z", "2026-03-29T00:00Z/2026-03-29T02:00Z"]


@pytest.mark.anyio
async def test_all_day_events_span_local_midnights_and_are_23_hours_on_the_change_day(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    document = calendar(
        event("winter", "2026-02-10", "2026-02-11", all_day=True),
        event("change", "2026-03-29", "2026-03-30", all_day=True),
        event("no-end", "2026-03-30", None, all_day=True),
        event("holiday", "2026-03-27", "2026-03-30", all_day=True),
        timezone="Europe/Berlin",
    )
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            events = await events_of(client)
            windows = await windows_of(
                client, range_from="2026-03-28T12:00Z", range_to="2026-03-30T12:00Z"
            )

    assert [(e["start"], e["end"], e["all_day"]) for e in events] == [
        ("2026-02-09T23:00Z", "2026-02-10T23:00Z", True),
        ("2026-03-26T23:00Z", "2026-03-29T22:00Z", True),
        ("2026-03-28T23:00Z", "2026-03-29T22:00Z", True),
        ("2026-03-29T22:00Z", "2026-03-30T22:00Z", True),
    ]
    # The days run into each other with neither a gap nor an hour of overlap too many.
    assert windows == [("2026-03-28T12:00Z", "2026-03-30T12:00Z", "busy")]


@pytest.mark.anyio
async def test_a_recurring_all_day_event_follows_the_calendar_day_across_the_change(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    document = calendar(
        event(
            "daily-off",
            "2026-03-27",
            "2026-03-28",
            all_day=True,
            recurrence={"freq": "daily", "count": 4},
            exceptions=["2026-03-29"],
        ),
        timezone="Europe/Berlin",
    )
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            spans = await spans_of(client)

    # 29 March is an exception, and still counts as one of the four occurrences.
    assert spans == [
        "2026-03-26T23:00Z/2026-03-27T23:00Z",
        "2026-03-27T23:00Z/2026-03-28T23:00Z",
        "2026-03-29T22:00Z/2026-03-30T22:00Z",
    ]


@pytest.mark.anyio
async def test_recurrence_rules_expand_inside_the_look_ahead_only(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    document = calendar(
        event(
            "mwf",
            "2026-01-12T09:00:00Z",
            "2026-01-12T09:30:00Z",
            recurrence={"freq": "weekly", "by_day": ["mo", "we", "fr"], "count": 5},
        ),
        event(
            "monthly",
            "2026-01-31T15:00:00Z",
            "2026-01-31T16:00:00Z",
            recurrence={"freq": "monthly", "until": "2026-06-30"},
        ),
        event(
            "every-other-day",
            "2026-03-30T06:00:00Z",
            "2026-03-30T06:15:00Z",
            recurrence={"freq": "daily", "interval": 2},
        ),
        event(
            "yearly",
            "2025-01-14T10:00:00Z",
            "2025-01-14T11:00:00Z",
            recurrence={"freq": "yearly"},
        ),
    )
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            starts = [e["start"] for e in await events_of(client, limit=50)]

    assert [s for s in starts if s.endswith("T09:00Z")] == [
        "2026-01-12T09:00Z",
        "2026-01-14T09:00Z",
        "2026-01-16T09:00Z",
        "2026-01-19T09:00Z",
        "2026-01-21T09:00Z",
    ]
    # The 31st: months without one are skipped.
    assert [s for s in starts if s.endswith("T15:00Z")] == [
        "2026-01-31T15:00Z",
        "2026-03-31T15:00Z",
    ]
    assert [s for s in starts if s.endswith("T06:00Z")] == [
        "2026-03-30T06:00Z",
        "2026-04-01T06:00Z",
        "2026-04-03T06:00Z",
        "2026-04-05T06:00Z",
        "2026-04-07T06:00Z",
        "2026-04-09T06:00Z",
    ]
    # The yearly event started in 2025; its occurrence in 2027 is beyond the look-ahead.
    assert [s for s in starts if s.endswith("T10:00Z")] == ["2026-01-14T10:00Z"]
    assert max(starts) <= "2026-04-10T12:00Z"


@pytest.mark.anyio
async def test_a_recurring_event_that_started_in_the_past_serves_its_future_occurrences(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    document = calendar(
        event(
            "standup",
            "2025-06-02T09:00:00",
            "2025-06-02T09:15:00",
            timezone="Europe/Berlin",
            recurrence={"freq": "weekly"},
        )
    )
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            first = await call(client, EVENTS_TOOL, limit=2)

    assert [(e["start"], e["end"]) for e in first["events"]] == [
        ("2026-01-12T08:00Z", "2026-01-12T08:15Z"),
        ("2026-01-19T08:00Z", "2026-01-19T08:15Z"),
    ]


# --- status and participation -----------------------------------------------------------


@pytest.mark.anyio
async def test_status_participation_and_transparency_map_to_a_closed_busy_status(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    cases: dict[str, tuple[dict[str, object], str]] = {
        "plain": ({}, "busy"),
        "confirmed": ({"status": "confirmed", "participation": "accepted"}, "busy"),
        "tentative": ({"status": "tentative"}, "tentative"),
        "maybe": ({"participation": "tentative"}, "tentative"),
        "unanswered": ({"participation": "needs_action"}, "tentative"),
        "declined": ({"participation": "declined"}, "free"),
        "cancelled": ({"status": "cancelled"}, "free"),
        "transparent": ({"transparency": "transparent"}, "free"),
        "bad_status": ({"status": "x-vendor-" + CORPUS["marker"]}, "unknown"),
        "bad_participation": ({"participation": CORPUS["injection"]}, "unknown"),
        "bad_transparency": ({"transparency": CORPUS["tool_call"]}, "unknown"),
        "odd_type": ({"status": ["confirmed"]}, "unknown"),
        "bad_and_free": ({"status": CORPUS["role_tag"], "transparency": "transparent"}, "unknown"),
    }
    events = [
        event(label, f"2026-01-{12 + index:02d}T09:00:00Z", f"2026-01-{12 + index:02d}T10:00:00Z")
        | fields
        for index, (label, (fields, _)) in enumerate(cases.items())
    ]
    with publish(start_gate, tmp_path, calendar(*events)) as gate:
        async with agent_client(gate) as client:
            listed = await events_of(client)
            windows = await windows_of(
                client, range_from="2026-01-12T00:00Z", range_to="2026-01-25T00:00Z"
            )
            channels = await observe_gate(gate)

    assert [e["busy_status"] for e in listed] == [status for _, status in cases.values()]
    # Only free events leave the time free: tentative and unknown ones hold it.
    held = sum(status != "free" for _, status in cases.values())
    assert len([w for w in windows if w[2] == "busy"]) == held
    assert_no_corpus(channels)


# --- limits and the shape of the tool surface -------------------------------------------


@pytest.mark.anyio
async def test_event_pages_hold_at_most_fifty_with_a_cursor_bound_to_token_and_query(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    first_day = datetime(2026, 2, 1)
    events = [
        timed(f"e{index}", first_day, timedelta(hours=index), timedelta(minutes=30))
        for index in range(120)
    ]
    source = write_fixture(tmp_path / "calendar.json", calendar(*events))
    with start_gate(
        accounts={"cal": source},
        kinds={"cal": CALENDAR},
        tokens={"a": ["cal"], "b": ["cal"]},
        token="a",
    ) as gate:
        async with agent_client(gate) as client:
            first = await call(client, EVENTS_TOOL)
            second = await call(client, EVENTS_TOOL, cursor=first["next_cursor"])
            third = await call(client, EVENTS_TOOL, cursor=second["next_cursor"])
            descending = await call(client, EVENTS_TOOL, sort="start_desc")
            wrong_sort = await call(
                client, EVENTS_TOOL, sort="start_desc", cursor=first["next_cursor"]
            )
            wrong_filter = await call(
                client, EVENTS_TOOL, busy_status="busy", cursor=first["next_cursor"]
            )
            wrong_tool = await call(client, OVERLAPS_TOOL, cursor=first["next_cursor"])
            resized = await call(client, EVENTS_TOOL, limit=10, cursor=first["next_cursor"])
            too_many = await call(client, EVENTS_TOOL, limit=51)
        async with agent_client(gate, gate.tokens["b"]) as other:
            foreign = await call(other, EVENTS_TOOL, cursor=first["next_cursor"])

    assert [len(page["events"]) for page in (first, second, third)] == [50, 50, 20]
    assert third["next_cursor"] is None
    handles = [e["event_handle"] for page in (first, second, third) for e in page["events"]]
    assert len(set(handles)) == 120
    assert [e["event_handle"] for e in descending["events"]] == handles[::-1][:50]
    assert {wrong_sort["error"], wrong_filter["error"], wrong_tool["error"], foreign["error"]} == {
        "invalid_cursor"
    }
    assert len(resized["events"]) == 10
    assert too_many["error"] == "invalid_arguments"


@pytest.mark.anyio
async def test_tool_inputs_are_closed_and_invalid_input_is_a_fixed_error(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, week_calendar()) as gate:
        async with agent_client(gate) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            answers = {
                tool: [
                    await call(client, tool, **arguments)
                    for arguments in (
                        {"query": "standup"},
                        {"range_from": "2026-01-12T09:00:30Z"},
                        {"range_from": "tomorrow"},
                        {"range_to": "2026-01-12"},
                        {"account_handle": "short"},
                        {"limit": 0},
                        {"cursor": "x"},
                    )
                ]
                for tool in CALENDAR_TOOLS
            }

    for name in CALENDAR_TOOLS:
        tool = tools[name]
        assert tool.annotations is not None and tool.annotations.read_only_hint is True
        assert tool.output_schema is not None
        assert closed_schema_problems(tool.output_schema) == []
        assert closed_schema_problems(tool.input_schema) == []
        for answer in answers[name]:
            assert answer["error"] == "invalid_arguments"
            assert answer["accounts"] == []
    assert set(answers[AVAILABILITY_TOOL][0]) == {"error", "accounts", "windows", "next_cursor"}
    assert set(answers[EVENTS_TOOL][0]) == {"error", "accounts", "events", "next_cursor"}


@pytest.mark.anyio
async def test_calendar_tools_do_not_serve_mail_and_item_tools_do_not_serve_calendars(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    mail_source = write_fixture(
        tmp_path / "mail.json",
        mailbox(
            [
                {
                    "id": "m1",
                    "from": "bob@example.test",
                    "received": "2026-01-05T09:00:00Z",
                    "subject": "S",
                    "body": "B",
                }
            ]
        ),
    )
    calendar_source = write_fixture(tmp_path / "calendar.json", week_calendar())
    with start_gate(
        accounts={"mail": mail_source, "cal": calendar_source},
        kinds={"cal": CALENDAR},
        tokens={"default": ["mail", "cal"]},
    ) as gate:
        async with agent_client(gate) as client:
            accounts = await call(client, ACCOUNTS_TOOL)
            items = await call(client, LIST_TOOL)
            counted = await call(client, COUNT_TOOL)
            events = await call(client, EVENTS_TOOL)
            handles = {a["source_kind"]: a["account_handle"] for a in accounts["accounts"]}
            mail_as_calendar = await call(
                client, EVENTS_TOOL, account_handle=handles["fixture_mail"]
            )
            calendar_as_mail = await call(client, LIST_TOOL, account_handle=handles[CALENDAR])

    assert sorted(handles) == [CALENDAR, "fixture_mail"]
    assert [i["source_kind"] for i in items["items"]] == ["fixture_mail"]
    assert counted["count"] == "1"
    assert {e["account_handle"] for e in events["events"]} == {handles[CALENDAR]}
    assert len(items["accounts"]) == 1 and len(events["accounts"]) == 1
    # A mail account's handle given to a calendar tool, or the reverse, selects nothing.
    assert mail_as_calendar["events"] == []
    assert calendar_as_mail["items"] == []


@pytest.mark.anyio
async def test_a_token_limited_to_one_operation_sees_only_that_calendar_tool(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    source = write_fixture(tmp_path / "calendar.json", week_calendar())
    with start_gate(
        accounts={"cal": source}, kinds={"cal": CALENDAR}, tokens={"default": ["cal"]}
    ) as gate:
        issued = operator(
            gate.db,
            *("token", "issue", "--account", "cal", "--operation", AVAILABILITY_TOOL),
            *("--label", "narrow"),
        )
        assert issued.returncode == 0, issued.stderr
        async with agent_client(gate, issued.stdout.strip()) as client:
            names = [tool.name for tool in (await client.list_tools()).tools]
            denied = await client.call_tool(EVENTS_TOOL, {})
            allowed = await call(client, AVAILABILITY_TOOL)

    assert names == [AVAILABILITY_TOOL]
    assert denied.structured_content is not None
    assert denied.structured_content["error"] == "unknown_tool"
    assert allowed["error"] == "none"


# --- failures ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_calendar_that_cannot_be_read_is_an_outage_not_an_empty_calendar(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with start_gate(
        accounts={"cal": tmp_path / "missing.json"},
        kinds={"cal": CALENDAR},
        tokens={"default": ["cal"]},
    ) as gate:
        async with agent_client(gate) as client:
            results = [await call(client, tool) for tool in CALENDAR_TOOLS]

    for result in results:
        assert result["error"] == "source_unavailable"
        (entry,) = result["accounts"]
        assert (entry["status"], entry["last_sync"]) == ("stale", None)
        assert not result.get("events") and not result.get("windows")


@pytest.mark.anyio
async def test_a_failed_sync_after_a_good_one_keeps_the_last_sync_time(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    source = write_fixture(tmp_path / "calendar.json", week_calendar())
    with start_gate(
        accounts={"cal": source}, kinds={"cal": CALENDAR}, tokens={"default": ["cal"]}
    ) as gate:
        async with agent_client(gate) as client:
            good = await call(client, EVENTS_TOOL)
            source.write_text("{not json")
            bad = await call(client, EVENTS_TOOL)

    assert good["error"] == "none" and len(good["events"]) == 4
    assert bad["error"] == "source_unavailable"
    assert (
        bad["accounts"][0]["last_sync"] == good["accounts"][0]["last_sync"] == "2026-01-10T12:00Z"
    )


@pytest.mark.anyio
async def test_events_that_cannot_be_understood_are_skipped_and_counted(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    both = ("2026-01-12T10:00:00Z", "2026-01-12T11:00:00Z")
    document = calendar(
        event("good", "2026-01-12T09:00:00Z", "2026-01-12T10:00:00Z"),
        event("no-zone", "2026-01-12T10:00:00", "2026-01-12T11:00:00"),
        event("no-end", "2026-01-12T09:00:00Z", None),
        event("backwards", "2026-01-12T10:00:00Z", "2026-01-12T09:00:00Z"),
        event("zero", "2026-01-12T10:00:00Z", "2026-01-12T10:00:30Z"),
        event("bad-time", CORPUS["injection"], "2026-01-12T11:00:00Z"),
        event("bad-zone", *both, timezone=CORPUS["path"]),
        event("bad-flag", "2026-01-12", "2026-01-13", all_day=CORPUS["marker"]),
        event("bad-rule", *both, recurrence="FREQ=DAILY"),
        event("bad-freq", *both, recurrence={"freq": "minutely"}),
        event("bad-count", *both, recurrence={"freq": "daily", "count": -1}),
        {"title": "no id", "start": both[0], "end": both[1]},
        "not an object",
    )
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            result = await call(client, EVENTS_TOOL)

    # A time with no offset and no time zone anywhere is read as UTC.
    assert [e["start"] for e in result["events"]] == ["2026-01-12T09:00Z", "2026-01-12T10:00Z"]
    (entry,) = result["accounts"]
    assert (entry["status"], entry["skipped_items"]) == ("incomplete", "10+")


@pytest.mark.anyio
async def test_a_document_that_is_not_a_calendar_is_an_outage(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    documents = [
        {"account": "a", "messages": []},
        {"events": []},
        {"account": "a", "events": {}},
        {"account": "a", "events": [], "timezone": CORPUS["path"]},
        [],
    ]
    for number, document in enumerate(documents):
        with publish(start_gate, tmp_path, document, db=f"shape{number}.db") as gate:
            async with agent_client(gate) as client:
                result = await call(client, EVENTS_TOOL)
        assert result["error"] == "source_unavailable", document


# --- identity, coverage and empty ranges ------------------------------------------------


@pytest.mark.anyio
async def test_a_standalone_event_cannot_take_the_identity_of_an_occurrence(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    series = event(
        "series",
        "2026-01-13T09:00:00Z",
        "2026-01-13T10:00:00Z",
        recurrence={"freq": "daily", "count": 2},
    )
    # A UID that spells the series UID, a separator and the day of its first occurrence.
    lookalike = event("series\x002026-01-13", "2026-01-13T14:00:00Z", "2026-01-13T15:00:00Z")
    day = {"range_from": "2026-01-13T08:00Z", "range_to": "2026-01-13T16:00Z"}
    with publish(start_gate, tmp_path, calendar(series, lookalike)) as gate:
        async with agent_client(gate) as client:
            first = await events_of(client)
            again = await events_of(client)
            windows = await windows_of(client, **day)
    with publish(start_gate, tmp_path, calendar(series, lookalike)) as restarted:
        async with agent_client(restarted) as client:
            after_restart = await events_of(client)

    assert [e["start"] for e in first] == [
        "2026-01-13T09:00Z",
        "2026-01-13T14:00Z",
        "2026-01-14T09:00Z",
    ]
    assert len({e["event_handle"] for e in first}) == 3
    assert first == again == after_restart
    assert windows == [
        ("2026-01-13T08:00Z", "2026-01-13T09:00Z", "free"),
        ("2026-01-13T09:00Z", "2026-01-13T10:00Z", "busy"),
        ("2026-01-13T10:00Z", "2026-01-13T14:00Z", "free"),
        ("2026-01-13T14:00Z", "2026-01-13T15:00Z", "busy"),
        ("2026-01-13T15:00Z", "2026-01-13T16:00Z", "free"),
    ]


@pytest.mark.anyio
async def test_events_that_share_a_uid_but_disagree_all_hold_their_time(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    document = calendar(
        event("same", "2026-01-12T09:00:00Z", "2026-01-12T10:00:00Z"),
        event("same", "2026-01-12T09:00:00Z", "2026-01-12T10:00:00Z"),
        event("twice", "2026-01-12T11:00:00Z", "2026-01-12T12:00:00Z"),
        event("twice", "2026-01-12T13:00:00Z", "2026-01-12T14:00:00Z"),
        event("twice", "2026-01-12T15:00:00Z", "2026-01-12T16:00:00Z"),
    )
    day = {"range_from": "2026-01-12T08:00Z", "range_to": "2026-01-12T17:00Z"}
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            result = await call(client, EVENTS_TOOL)
            windows = await windows_of(client, **day)
            again = await events_of(client)

    # A repeat of the same event is one event; a different event under the same UID is
    # another, never dropped, so its time cannot be reported free.
    starts = [e["start"] for e in result["events"]]
    assert starts == [f"2026-01-12T{hour}:00Z" for hour in ("09", "11", "13", "15")]
    assert len({e["event_handle"] for e in result["events"]}) == 4
    assert again == result["events"]
    assert [w[2] for w in windows] == [
        "free",
        "busy",
        "free",
        "busy",
        "free",
        "busy",
        "free",
        "busy",
        "free",
    ]
    (entry,) = result["accounts"]
    assert (entry["status"], entry["skipped_items"]) == ("current", "0")


@pytest.mark.anyio
async def test_reordering_events_that_share_a_uid_keeps_each_handle_on_its_event(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    morning = event("shared", "2026-01-12T09:00:00Z", "2026-01-12T10:00:00Z")
    evening = event("shared", "2026-01-12T17:00:00Z", "2026-01-12T18:00:00Z")
    source = write_fixture(tmp_path / "calendar.json", calendar(morning, evening))
    with start_gate(
        accounts={"cal": source}, kinds={"cal": CALENDAR}, tokens={"default": ["cal"]}
    ) as gate:
        async with agent_client(gate) as client:
            before = await events_of(client)
            write_fixture(source, calendar(evening, morning))
            reordered = await events_of(client)
    with start_gate(
        accounts={"cal": source}, kinds={"cal": CALENDAR}, tokens={"default": ["cal"]}
    ) as restarted:
        async with agent_client(restarted) as client:
            after_restart = await events_of(client)

    assert [e["start"] for e in before] == ["2026-01-12T09:00Z", "2026-01-12T17:00Z"]
    assert len({e["event_handle"] for e in before}) == 2
    # Each handle stays with the time block it had, whichever entry comes first.
    assert reordered == before
    assert after_restart == before


@pytest.mark.anyio
async def test_a_series_that_cannot_be_expanded_leaves_its_time_unknown_not_free(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    # A daily series from 1960 with no end: ongoing, but too far back to expand from its start.
    document = calendar(
        event(
            "ancient",
            "1960-01-01T09:00:00Z",
            "1960-01-01T10:00:00Z",
            recurrence={"freq": "daily"},
        ),
        event("known", "2026-01-12T12:00:00Z", "2026-01-12T13:00:00Z"),
    )
    day = {"range_from": "2026-01-12T00:00Z", "range_to": "2026-01-13T00:00Z"}
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            windows = await windows_of(client, **day)
            listed = await call(client, EVENTS_TOOL)

    # The series' own occurrences are not known, so no time is reported free; the event that
    # is known still holds its time.
    assert windows == [
        ("2026-01-12T00:00Z", "2026-01-12T12:00Z", "unknown"),
        ("2026-01-12T12:00Z", "2026-01-12T13:00Z", "busy"),
        ("2026-01-12T13:00Z", "2026-01-13T00:00Z", "unknown"),
    ]
    assert [e["start"] for e in listed["events"]] == ["2026-01-12T12:00Z"]
    (entry,) = listed["accounts"]
    assert (entry["status"], entry["skipped_items"]) == ("incomplete", "1")


@pytest.mark.anyio
async def test_events_dropped_by_the_storage_cap_leave_their_time_unknown_not_free(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    first_day = datetime(2026, 1, 11)
    events = [
        timed(f"e{index}", first_day, timedelta(hours=index), timedelta(minutes=30))
        for index in range(MAX_EVENTS + 100)
    ]
    cutoff = first_day + timedelta(hours=MAX_EVENTS)  # where the first dropped event starts

    def at(offset: timedelta) -> str:
        return (cutoff + offset).strftime("%Y-%m-%dT%H:%MZ")

    hour, half = timedelta(hours=1), timedelta(minutes=30)
    with publish(start_gate, tmp_path, calendar(*events)) as gate:
        async with agent_client(gate) as client:
            windows = await windows_of(client, range_from=at(-2 * hour), range_to=at(4 * hour))
            whole = await call(client, AVAILABILITY_TOOL, limit=50)
            last = await windows_of(client, range_from=at(5 * hour), range_to=LOOK_AHEAD_END)
            listed = await call(client, EVENTS_TOOL, range_from=at(-hour), range_to=at(4 * hour))

    # The kept events are served as they are; from where the first one was dropped, the
    # source is not known to be free.
    assert windows == [
        (at(-2 * hour), at(-2 * hour + half), "busy"),
        (at(-2 * hour + half), at(-hour), "free"),
        (at(-hour), at(-hour + half), "busy"),
        (at(-hour + half), at(timedelta(0)), "free"),
        (at(timedelta(0)), at(4 * hour), "unknown"),
    ]
    assert last == [(at(5 * hour), LOOK_AHEAD_END, "unknown")]
    assert whole["windows"][0] == {
        "start": "2026-01-10T12:00Z",
        "end": "2026-01-11T00:00Z",
        "state": "free",
    }
    assert len(listed["events"]) == 1
    (entry,) = listed["accounts"]
    assert (entry["status"], entry["skipped_items"]) == ("incomplete", "10+")


@pytest.mark.anyio
async def test_a_range_with_no_time_inside_the_look_ahead_selects_nothing_from_any_tool(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    # Two events that overlap each other and are running when the gate's clock is 12:00.
    document = calendar(
        event("a", "2026-01-10T11:00:00Z", "2026-01-10T13:00:00Z"),
        event("b", "2026-01-10T11:30:00Z", "2026-01-10T13:30:00Z"),
        event("c", "2026-04-10T11:00:00Z", "2026-04-10T13:00:00Z"),
    )
    empty_ranges: list[dict[str, str]] = [
        {"range_from": "2025-01-01T00:00Z", "range_to": "2026-01-10T11:45Z"},
        {"range_to": "2026-01-10T11:45Z"},
        {"range_from": "2026-01-10T12:00Z", "range_to": "2026-01-10T12:00Z"},
        {"range_from": "2026-01-11T00:00Z", "range_to": "2026-01-10T00:00Z"},
        {"range_from": "2026-04-10T12:00Z", "range_to": "2026-06-01T00:00Z"},
        {"range_from": "2026-04-10T12:00Z"},
    ]
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            answers = {
                (tool, json.dumps(arguments)): await call(client, tool, **arguments)
                for tool in CALENDAR_TOOLS
                for arguments in empty_ranges
            }
            running_events = await events_of(client, range_to="2026-01-10T12:30Z")
            running_overlaps = await spans_of(client, OVERLAPS_TOOL, range_to="2026-01-10T12:30Z")

    for key, answer in answers.items():
        assert answer["error"] == "none", key
        assert not answer.get("events") and not answer.get("windows"), key
    # The same events are served for a range that does include the present.
    assert len(running_events) == 2
    assert running_overlaps == [
        "2026-01-10T11:00Z/2026-01-10T13:00Z",
        "2026-01-10T11:30Z/2026-01-10T13:30Z",
    ]


# --- retention, handles and the hostile corpus ------------------------------------------


@pytest.mark.anyio
async def test_no_corpus_string_reaches_any_channel_from_a_hostile_calendar(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, hostile_calendar_fixture()) as gate:
        channels = await observe_gate(gate)
        async with agent_client(gate) as client:
            events = await events_of(client, limit=50)

    assert len(events) >= len(CORPUS)
    assert {"call-get_availability", "call-list_events", "call-list_overlaps"} <= set(channels)
    assert_no_corpus(channels)
    for entry in events:
        assert set(entry) == EVENT_FIELDS
        assert entry["busy_status"] in BUSY_STATUSES


@pytest.mark.anyio
async def test_raw_event_text_is_not_persisted(start_gate: GateStarter, tmp_path: Path) -> None:
    """Below the seam on purpose: retention is a property of the stored files."""
    with publish(start_gate, tmp_path, hostile_calendar_fixture()) as gate:
        async with agent_client(gate) as client:
            events = await events_of(client, limit=50)

    files = storage_files(gate.db)

    assert events
    assert_no_corpus(files)
    assert b"evil.test" not in b"".join(files.values())
    with sqlite3.connect(gate.db) as database:
        columns = {row[1] for row in database.execute("pragma table_info(events)")}
        stored = database.execute("select count(*) from events").fetchone()[0]
    assert columns == {
        "event_handle",
        "account_handle",
        "start_time",
        "end_time",
        "all_day",
        "busy_status",
    }
    assert stored == len(events)


@pytest.mark.anyio
async def test_event_handles_are_stable_across_calls_and_restarts_and_unique_per_occurrence(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    document = calendar(
        event("a", "2026-01-12T09:00:00Z", "2026-01-12T10:00:00Z"),
        event(
            "series",
            "2026-01-13T09:00:00Z",
            "2026-01-13T10:00:00Z",
            recurrence={"freq": "daily", "count": 3},
        ),
    )
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            first = await events_of(client)
            again = await events_of(client)
    with publish(start_gate, tmp_path, document) as restarted:
        async with agent_client(restarted) as client:
            after_restart = await events_of(client)

    assert first == again == after_restart
    assert len({e["event_handle"] for e in first}) == 4


@pytest.mark.anyio
async def test_an_event_that_moves_keeps_its_handle_and_a_removed_one_disappears(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    source = write_fixture(
        tmp_path / "calendar.json",
        calendar(event("a", "2026-01-12T09:00:00Z", "2026-01-12T10:00:00Z")),
    )
    with start_gate(
        accounts={"cal": source}, kinds={"cal": CALENDAR}, tokens={"default": ["cal"]}
    ) as gate:
        async with agent_client(gate) as client:
            before = await events_of(client)
            write_fixture(
                source,
                calendar(
                    event("a", "2026-01-12T11:00:00Z", "2026-01-12T12:00:00Z"),
                    event("b", "2026-01-12T13:00:00Z", "2026-01-12T14:00:00Z"),
                ),
            )
            moved = await events_of(client)
            write_fixture(source, calendar())
            emptied = await events_of(client)

    assert moved[0]["event_handle"] == before[0]["event_handle"]
    assert [e["start"] for e in moved] == ["2026-01-12T11:00Z", "2026-01-12T13:00Z"]
    assert emptied == []


@pytest.mark.anyio
async def test_the_fixture_cast_calendar_is_served_through_the_running_gate(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    state = tmp_path / "seed"
    assert run_seed(state).returncode == 0
    cast = json.loads(CAST.read_text())["services"]["mailbox-calendar"]
    with serve("mailbox-calendar", state) as source_url:
        with start_gate(
            accounts={"cal": source_url},
            kinds={"cal": CALENDAR},
            tokens={"default": ["cal"]},
            now="2026-01-06T00:00:00Z",
        ) as gate:
            channels = await observe_gate(gate)
            async with agent_client(gate) as client:
                spans = await spans_of(client)

    assert spans == [
        "2026-01-07T09:00Z/2026-01-07T09:30Z",
        "2026-01-07T13:00Z/2026-01-07T15:00Z",
    ]
    served = [cast["account"]]
    for entry in cast["events"]:
        served += [entry["id"], entry["organizer"], entry["title"]]
    everything: dict[str, str | bytes] = {**channels, **storage_files(gate.db)}
    for name, content in everything.items():
        text = content if isinstance(content, str) else content.decode("utf-8", "replace")
        assert not any(value in text for value in served), name
    assert_no_corpus(everything)
