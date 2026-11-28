"""Acceptance tests of the Office 365 mail source (ADR 0002, 0003, 0004, 0006, 0007, ticket 16).

Everything goes through MCP tool calls from a real client against a running gate whose
`o365_mail` account talks to the Microsoft stand-in of `tests/o365.py`: an https listener that
plays the authority (OIDC discovery, token and device code endpoints, rotating refresh tokens)
and Graph, and records every request. The login in the credentials file is made by the
operator's own command, `glovebox o365-login`, against that stand-in. A few tests reach below
the seam and say so: the requests the stand-in recorded, the credentials file the gate rewrites
and the adapter's own limits, none of which an agent can see. What real Microsoft accounts
do, including the employer's consent, is checked by the operator (docs/o365.md). One fault at
a time; simultaneous faults are out of scope.
"""

import json
import os
import stat
from collections.abc import Iterator
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from glovebox import o365
from glovebox.credentials import CredentialsError, read_credentials, write_credentials
from glovebox.sources import SourceBlocked, SourceError
from tests.client import ACCOUNTS_TOOL, COUNT_TOOL, GROUP_TOOL, LIST_TOOL, agent_client
from tests.conftest import GateStarter, RunningGate, operator
from tests.hostile import CORPUS, assert_no_corpus, observe_gate
from tests.o365 import (
    CLIENT_ID,
    GRAPH_FAULTS,
    GRAPH_PATH,
    TOKEN_FAULTS,
    Microsoft,
    certificate,
    graph_message,
    microsoft_serving,
    trust_environment,
)
from tests.test_failures import account_of, assert_clean
from tests.test_gate import small_fixture, storage_files, write_fixture
from tests.test_inference import OTHER, TRAVEL, seed_state, standin
from tests.test_queries import call

O365 = "o365_mail"
WORK = "work.example.test"
FIRST_SYNC = "2026-01-10T12:00Z"
# The gate's clock is pinned to 2026-01-10T12:00Z, so the window starts 2025-12-11T12:00Z.
WINDOW_START = datetime(2025, 12, 11, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
MAIL_PERMISSIONS = {"mail.read", "openid", "profile", "offline_access"}


def triage() -> list[dict[str, Any]]:
    """Three messages in the window, two of them in one conversation, and one older."""
    return [
        graph_message(
            "AAMk-a1", f"bob@{WORK}", "2026-01-05T09:00:59Z", read=True, conversation="conv-1"
        ),
        graph_message(
            "AAMk-a2",
            f"bob@{WORK}",
            "2026-01-05T09:05:00Z",
            read=False,
            attachments=True,
            conversation="conv-1",
        ),
        graph_message("AAMk-b1", f"carol@{WORK}", "2026-01-08T16:30:10Z", conversation="conv-2"),
        graph_message("AAMk-old", f"bob@{WORK}", "2025-11-01T10:00:00Z", read=True),
    ]


@pytest.fixture(scope="session")
def trusted_certificate(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return certificate(tmp_path_factory.mktemp("o365-certificate"))


@pytest.fixture
def microsoft(trusted_certificate: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Microsoft]:
    """The Microsoft stand-in, trusted by this process and by every gate and operator command
    it starts, which inherit the environment."""
    for name, value in trust_environment(trusted_certificate).items():
        monkeypatch.setenv(name, value)
    with microsoft_serving(trusted_certificate, triage()) as stand_in:
        yield stand_in


def sign_in(stand_in: Microsoft, tmp_path: Path, name: str = "o365-login") -> Path:
    """The operator's login: `glovebox o365-login` against the stand-in."""
    credentials = tmp_path / name
    result = operator(
        tmp_path / "login.db",
        *("o365-login", "--client-id", CLIENT_ID, "--authority", stand_in.authority),
        *("--credentials", str(credentials)),
    )
    assert result.returncode == 0, result.stderr
    stand_in.requests.clear()
    stand_in.token_forms.clear()
    return credentials


def add_account(tmp_path: Path, stand_in: Microsoft, credentials: Path, name: str = "work") -> None:
    added = operator(
        tmp_path / "glovebox.db",
        *("account", "add", name, "--kind", O365, "--source", stand_in.graph),
        *("--credentials", str(credentials)),
    )
    assert added.returncode == 0, added.stderr


@pytest.fixture
def signed_in(microsoft: Microsoft, tmp_path: Path) -> Path:
    """The credentials file of an operator who signed in, with the account added."""
    credentials = sign_in(microsoft, tmp_path)
    add_account(tmp_path, microsoft, credentials)
    return credentials


def work_gate(
    start_gate: GateStarter, stand_in: Microsoft, **options: Any
) -> AbstractContextManager[RunningGate]:
    return start_gate(accounts={"work": stand_in.graph}, kinds={"work": O365}, **options)


def served(structured: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = structured["items"]
    return items


# --- serving mail --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_work_mail_is_served_as_items_with_handles_and_permitted_fields(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path
) -> None:
    with work_gate(start_gate, microsoft) as gate:
        async with agent_client(gate) as client:
            listing = await call(client, LIST_TOOL)
            count = await call(client, COUNT_TOOL, read_status="unread")
            threads = await call(client, GROUP_TOOL, group_by="thread_handle")
            senders = await call(client, GROUP_TOOL, group_by="sender_handle")
        channels = {"listing": json.dumps(listing), "log": gate.log.read_text(errors="replace")}
        channels.update(
            {
                name: content.decode("utf-8", "replace")
                for name, content in storage_files(gate.db).items()
            }
        )

    items = served(listing)
    # The message older than 30 days is not ingested; the rest is newest first.
    assert [item["received"] for item in items] == [
        "2026-01-08T16:30Z",
        "2026-01-05T09:05Z",
        "2026-01-05T09:00Z",
    ]
    assert [item["read_status"] for item in items] == ["unread", "unread", "read"]
    assert [item["has_attachments"] for item in items] == [False, True, False]
    assert {item["source_kind"] for item in items} == {O365}
    assert items[1]["thread_handle"] == items[2]["thread_handle"] != items[0]["thread_handle"]
    assert items[1]["sender_handle"] == items[2]["sender_handle"] != items[0]["sender_handle"]
    assert count["count"] == "2"
    assert sorted(group["count"] for group in threads["groups"]) == ["1", "2"]
    assert sorted(group["count"] for group in senders["groups"]) == ["1", "2"]
    account = account_of(listing)
    assert (account["status"], account["last_sync"], account["skipped_items"]) == (
        "current",
        FIRST_SYNC,
        "0",
    )
    # No upstream value is served or kept: IDs, addresses, subjects, bodies, conversations.
    upstream = ["AAMk-a1", "conv-1", "bob@", "carol@", "SUBJ-5821", "BODY-5821", "SNAME-5821"]
    for name, text in channels.items():
        assert not any(value in text for value in upstream), name


@pytest.mark.anyio
async def test_a_message_received_after_the_first_read_shows_up_on_the_next(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path
) -> None:
    with work_gate(start_gate, microsoft) as gate:
        async with agent_client(gate) as client:
            before = await call(client, COUNT_TOOL)
            microsoft.messages.append(
                graph_message("new", f"dave@{WORK}", "2026-01-09T10:00:00Z", conversation="c3")
            )
            after = await call(client, COUNT_TOOL)
            microsoft.messages[:] = [m for m in microsoft.messages if m["id"] != "AAMk-a1"]
            gone = await call(client, COUNT_TOOL)

    assert (before["count"], after["count"], gone["count"]) == ("3", "4", "3")


@pytest.mark.anyio
async def test_every_page_of_a_large_window_is_read(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path
) -> None:
    microsoft.page_size = 7
    microsoft.messages = [
        graph_message(f"m{index}", f"bob@{WORK}", f"2026-01-05T08:{index:02d}:00Z")
        for index in range(30)
    ]
    with work_gate(start_gate, microsoft) as gate:
        async with agent_client(gate) as client:
            count = await call(client, COUNT_TOOL)

    assert count["count"] == "30"
    assert len(microsoft.graph_requests()) == 5


@pytest.mark.anyio
async def test_an_unreadable_message_is_skipped_and_counted_not_served(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path
) -> None:
    broken = [
        {**graph_message("x1", f"bob@{WORK}", "2026-01-05T08:00:00Z"), "receivedDateTime": "no"},
        {**graph_message("x2", f"bob@{WORK}", "2026-01-05T08:01:00Z"), "hasAttachments": "yes"},
        {**graph_message("x3", f"bob@{WORK}", "2026-01-05T08:02:00Z"), "from": None},
        {**graph_message("x4", f"bob@{WORK}", "2026-01-05T08:03:00Z"), "id": ""},
    ]
    microsoft.messages.extend(broken)
    with work_gate(start_gate, microsoft) as gate:
        async with agent_client(gate) as client:
            listing = await call(client, LIST_TOOL)

    assert len(served(listing)) == 3
    assert "x1" not in json.dumps(listing)
    account = account_of(listing)
    assert (account["status"], account["skipped_items"]) == ("incomplete", "4")


def test_something_that_is_not_a_message_is_not_an_item() -> None:
    assert o365.parse_graph_message("a message") is None
    assert o365.parse_graph_message(["id", "from"]) is None
    assert o365.parse_graph_message({}) is None


# --- read-only, enforced by the provider ----------------------------------------------------


@pytest.mark.anyio
async def test_the_account_reports_that_read_only_is_enforced_by_the_provider(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path
) -> None:
    with work_gate(start_gate, microsoft) as gate:
        async with agent_client(gate) as client:
            accounts = await call(client, ACCOUNTS_TOOL)
            listing = await call(client, LIST_TOOL)

    for result in (accounts, listing):
        (entry,) = result["accounts"]
        assert entry["read_only_enforcement"] == "provider"


@pytest.mark.anyio
async def test_the_gate_asks_only_for_read_permission_and_only_ever_reads(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path
) -> None:
    unread = [m["isRead"] for m in microsoft.messages]
    with work_gate(start_gate, microsoft) as gate:
        async with agent_client(gate) as client:
            tools = (await client.list_tools()).tools
            await call(client, ACCOUNTS_TOOL)
            await call(client, LIST_TOOL)
            await call(client, COUNT_TOOL, read_status="unread")
            await call(client, GROUP_TOOL, group_by="source_kind")

    # What the token is asked to be good for: Mail.Read, and what MSAL adds to keep the login.
    scopes = {scope.lower() for form in microsoft.token_forms for scope in form["scope"].split()}
    assert scopes == MAIL_PERMISSIONS
    assert {form["grant_type"] for form in microsoft.token_forms} == {"refresh_token"}
    # At Graph: GET of the inbox's messages, with nothing else in the query, nothing else sent.
    graph = microsoft.graph_requests()
    assert len(graph) == 3  # the item tools read the source; the account listing does not
    assert {seen.method for seen in graph} == {"GET"}
    for seen in graph:
        parts = urlsplit(seen.path)
        assert parts.path == GRAPH_PATH
        assert set(parse_qs(parts.query)) == {"$select", "$filter", "$orderby", "$top"}
        assert seen.authorization is not None and seen.authorization.startswith("Bearer access-")
        assert seen.prefer is not None and "ImmutableId" in seen.prefer
        assert 'body-content-type="text"' in seen.prefer
    # The filter is the 30-day window of the gate's clock.
    (clause,) = parse_qs(urlsplit(graph[0].path).query)["$filter"]
    assert clause == "receivedDateTime ge 2025-12-11T12:00:00Z"
    # Every request of the stand-in's was a read of Graph or a token or discovery exchange.
    assert {(seen.method, seen.path.split("?")[0]) for seen in microsoft.requests} <= {
        ("GET", GRAPH_PATH),
        ("GET", "/tenant-0001/v2.0/.well-known/openid-configuration"),
        ("POST", "/tenant-0001/oauth2/v2.0/token"),
    }
    # Reading marks nothing read upstream, and no tool of the gate can.
    assert [m["isRead"] for m in microsoft.messages] == unread
    assert not any(tool.annotations and tool.annotations.read_only_hint is False for tool in tools)


@pytest.mark.anyio
@pytest.mark.parametrize("granted", ["Mail.ReadWrite", "Mail.Send", "Mail.Read.Write.All"])
async def test_a_token_that_could_write_is_refused_before_anything_is_read(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path, granted: str
) -> None:
    microsoft.scope = f"Mail.Read {granted} openid profile"
    with work_gate(start_gate, microsoft) as gate:
        async with agent_client(gate) as client:
            result = await call(client, LIST_TOOL)

    assert result["error"] == "source_unavailable"
    assert account_of(result)["status"] == "blocked"
    assert microsoft.graph_requests() == []


@pytest.mark.anyio
async def test_a_token_with_other_read_permissions_is_used(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path
) -> None:
    microsoft.scope = (
        "https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Calendars.Read "
        "https://graph.microsoft.com/User.Read openid profile email"
    )
    with work_gate(start_gate, microsoft) as gate:
        async with agent_client(gate) as client:
            result = await call(client, COUNT_TOOL)

    assert result["count"] == "3"


# --- the login: refresh, rotation and the credentials file -----------------------------------


@pytest.mark.anyio
async def test_a_cached_access_token_is_reused_while_it_lasts(
    start_gate: GateStarter, microsoft: Microsoft, tmp_path: Path
) -> None:
    microsoft.expires_in = 3600
    add_account(tmp_path, microsoft, sign_in(microsoft, tmp_path))
    with work_gate(start_gate, microsoft) as gate:
        async with agent_client(gate) as client:
            await call(client, COUNT_TOOL)
            await call(client, COUNT_TOOL)

    # The sign-in left an hour-long access token in the cache, which two reads share.
    assert microsoft.token_requests() == []
    assert len(microsoft.graph_requests()) == 2


@pytest.mark.anyio
async def test_a_rotated_refresh_token_is_saved_so_the_login_outlives_the_old_one(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path
) -> None:
    with work_gate(start_gate, microsoft) as gate:
        async with agent_client(gate) as client:
            counts = [(await call(client, COUNT_TOOL))["count"] for _ in range(3)]

    # Each read refreshed, and Microsoft retired the refresh token it was given each time.
    assert counts == ["3", "3", "3"]
    assert len(microsoft.token_requests()) == 3
    used = [form["refresh_token"] for form in microsoft.token_forms]
    assert len(set(used)) == 3
    saved = read_credentials(signed_in).reveal()
    assert microsoft.issued == 4 and "refresh-4" in saved
    assert "refresh-1" not in saved
    assert stat.S_IMODE(signed_in.stat().st_mode) == 0o600
    assert sorted(path.name for path in signed_in.parent.glob(f".{signed_in.name}*")) == []


@pytest.mark.anyio
async def test_tokens_reach_no_channel_log_or_database(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path
) -> None:
    with work_gate(start_gate, microsoft) as gate:
        channels = await observe_gate(gate)
        async with agent_client(gate) as client:
            channels["listing"] = json.dumps(await call(client, LIST_TOOL))
        channels.update(
            {name: data.decode("utf-8", "replace") for name, data in storage_files(gate.db).items()}
        )

    secrets = [f"access-{n}" for n in range(1, microsoft.issued + 1)]
    secrets += [f"refresh-{n}" for n in range(1, microsoft.issued + 1)] + [CLIENT_ID]
    assert microsoft.issued > 2
    for name, text in channels.items():
        assert not any(secret in text for secret in secrets), name


# --- consent: missing, withdrawn, and the other accounts ------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    "fault",
    ["consent_withdrawn", "interaction_required", "consent_required", "unauthorized_client"],
)
async def test_missing_or_withdrawn_consent_blocks_the_account_and_no_other(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path, tmp_path: Path, fault: str
) -> None:
    other = write_fixture(tmp_path / "other.json", small_fixture())
    accounts: dict[str, Path | str] = {"work": microsoft.graph, "home": other}
    kinds = {"work": O365}
    with start_gate(
        accounts=accounts, kinds=kinds, tokens={"both": ["work", "home"], "home": ["home"]}
    ) as gate:
        async with agent_client(gate, gate.tokens["both"]) as both:
            well = await call(both, COUNT_TOOL)
            microsoft.token_fault = fault
            down = await call(both, COUNT_TOOL)
            async with agent_client(gate, gate.tokens["home"]) as home:
                unaffected = await call(home, COUNT_TOOL)
            microsoft.token_fault = None
            up = await call(both, COUNT_TOOL)

    def entries(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {entry["source_kind"]: entry for entry in result["accounts"]}

    work, home_before = entries(well)[O365], entries(well)["fixture_mail"]
    assert (work["status"], home_before["status"]) == ("current", "current")
    assert down["error"] == "source_unavailable"
    assert entries(down) == {O365: {**work, "status": "blocked"}, "fixture_mail": home_before}
    assert (unaffected["error"], unaffected["count"]) == ("none", "3")
    assert up == well
    # Consent was withdrawn at the authority: nothing was asked of Graph with a stale token.
    assert len(microsoft.graph_requests()) == 2
    assert_clean(gate, well, down, unaffected, up)


@pytest.mark.anyio
async def test_a_login_that_never_signed_in_is_blocked(
    start_gate: GateStarter, microsoft: Microsoft, tmp_path: Path
) -> None:
    credentials = tmp_path / "o365-login"
    login = o365.Login(CLIENT_ID, microsoft.authority, "{}")
    write_credentials(credentials, login.text())
    add_account(tmp_path, microsoft, credentials)
    with work_gate(start_gate, microsoft) as gate:
        async with agent_client(gate) as client:
            result = await call(client, LIST_TOOL)

    assert result["error"] == "source_unavailable"
    account = account_of(result)
    assert (account["status"], account["last_sync"]) == ("blocked", None)
    assert microsoft.token_requests() == [] and microsoft.graph_requests() == []


@pytest.mark.anyio
async def test_a_credentials_file_that_is_not_a_login_is_blocked(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path
) -> None:
    signed_in.write_text(CORPUS["marker"])
    with work_gate(start_gate, microsoft) as gate:
        async with agent_client(gate) as client:
            result = await call(client, LIST_TOOL)

    assert account_of(result)["status"] == "blocked"
    assert microsoft.requests == []
    assert_clean(gate, result)


# --- outage, throttling, authentication failure and malformed responses ---------------------


async def current_then(
    gate: RunningGate, microsoft: Microsoft, **fault: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Read once while well, again with the fault, and once more after it is mended."""
    async with agent_client(gate) as client:
        well = await call(client, LIST_TOOL)
        for name, value in fault.items():
            setattr(microsoft, name, value)
        down = await call(client, COUNT_TOOL)
        for name in fault:
            setattr(microsoft, name, None)
        up = await call(client, LIST_TOOL)
    return well, down, up, account_of(down)


GRAPH_OUTAGES = [
    *(name for name, status in GRAPH_FAULTS.items() if status not in (401, 403)),
    "garbage",
    "wrong_shape",
    "not_a_list",
    "empty",
    "redirect",
    "reset",
    "truncated",
    "foreign_link",
]
TOKEN_OUTAGES = ["unavailable", "throttled", "server_error", "garbage", "wrong_shape", "reset"]


@pytest.mark.anyio
@pytest.mark.parametrize("fault", GRAPH_OUTAGES)
async def test_a_graph_outage_is_the_fixed_error_with_stale_status_and_ends_by_itself(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path, fault: str
) -> None:
    with work_gate(start_gate, microsoft) as gate:
        well, down, up, entry = await current_then(gate, microsoft, graph_fault=fault)

    assert down["error"] == "source_unavailable" and down["count"] == "0"
    # Never an apparently complete empty result: stale, with the last good sync time.
    assert entry == {**account_of(well), "status": "stale"}
    assert up == well
    log = gate.log.read_text(errors="replace")
    assert log.count("account work sync failed: stale") == 1
    assert_clean(gate, well, down, up)


@pytest.mark.anyio
@pytest.mark.parametrize("fault", TOKEN_OUTAGES)
async def test_an_authority_outage_is_the_fixed_error_with_stale_status_and_ends_by_itself(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path, fault: str
) -> None:
    with work_gate(start_gate, microsoft) as gate:
        well, down, up, entry = await current_then(gate, microsoft, token_fault=fault)

    assert down["error"] == "source_unavailable" and down["count"] == "0"
    assert entry == {**account_of(well), "status": "stale"}
    assert up == well
    assert_clean(gate, well, down, up)


@pytest.mark.anyio
@pytest.mark.parametrize("fault", ["unauthorized", "forbidden"])
async def test_graph_refusing_the_token_is_blocked_and_recovers_when_access_returns(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path, fault: str
) -> None:
    with work_gate(start_gate, microsoft) as gate:
        well, down, up, entry = await current_then(gate, microsoft, graph_fault=fault)

    assert down["error"] == "source_unavailable"
    assert entry == {**account_of(well), "status": "blocked"}
    assert up == well
    assert_clean(gate, well, down, up)


@pytest.mark.anyio
async def test_a_token_that_graph_refused_is_not_served_from_the_cache_again(
    start_gate: GateStarter, microsoft: Microsoft, tmp_path: Path
) -> None:
    microsoft.expires_in = 3600  # the cached access token would last an hour
    add_account(tmp_path, microsoft, sign_in(microsoft, tmp_path))
    with work_gate(start_gate, microsoft) as gate:
        async with agent_client(gate) as client:
            microsoft.graph_fault = "unauthorized"
            refused = await call(client, COUNT_TOOL)
            microsoft.graph_fault = None
            after = await call(client, COUNT_TOOL)

    assert account_of(refused)["status"] == "blocked"
    assert (after["error"], after["count"]) == ("none", "3")
    # The refused token was dropped, so the recovery asked the authority for another.
    assert len(microsoft.token_requests()) == 1


@pytest.mark.anyio
async def test_a_gate_restart_keeps_the_login(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path
) -> None:
    with work_gate(start_gate, microsoft) as gate:
        async with agent_client(gate) as client:
            first = await call(client, COUNT_TOOL)
    with work_gate(start_gate, microsoft) as gate:
        async with agent_client(gate) as client:
            second = await call(client, COUNT_TOOL)

    assert first["count"] == second["count"] == "3"
    assert account_of(second)["status"] == "current"


# --- the hostile corpus -----------------------------------------------------------------------


def hostile_graph_messages() -> list[dict[str, Any]]:
    """Graph messages with a corpus string in every property an outsider writes.

    One message per corpus string carries it in the sender's name and address, the other
    recipients, subject, preview, body, categories, flags, every upstream ID and etag,
    attachments and extra properties, with a valid time. A second per string carries it as the
    raw time, and a third as the attachment flag, which no gate can use.
    """
    messages: list[dict[str, Any]] = []
    for index, text in enumerate(CORPUS.values()):
        messages.append(
            {
                "@odata.etag": text,
                "@odata.id": f"https://evil.test/{text}",
                "id": f"id-{text}",
                "conversationId": f"thread-{text}",
                "conversationIndex": text,
                "internetMessageId": f"<{text}@evil.test>",
                "changeKey": text,
                "parentFolderId": text,
                "from": {"emailAddress": {"name": text, "address": f"{text}@evil.test"}},
                "sender": {"emailAddress": {"name": text, "address": text}},
                "toRecipients": [{"emailAddress": {"name": text, "address": text}}],
                "ccRecipients": [{"emailAddress": {"name": text, "address": text}}],
                "replyTo": [{"emailAddress": {"name": text, "address": text}}],
                "subject": text,
                "bodyPreview": text,
                "body": {"contentType": text, "content": text},
                "categories": [text],
                "flag": {"flagStatus": text},
                "importance": text,
                "webLink": text,
                "sentDateTime": text,
                "receivedDateTime": f"2026-01-08T08:{index:02d}:37Z",
                "isRead": text if index % 2 else False,
                "hasAttachments": index % 2 == 0,
                "attachments": [{"name": text, "contentType": text, "contentId": text}],
            }
        )
    for text in CORPUS.values():
        base = graph_message(f"bad-date-{text}", f"{text}@evil.test", text, subject=text, body=text)
        messages.append(base)
        flag = graph_message(
            f"bad-flag-{text}", f"{text}@evil.test", "2026-01-08T07:00:00Z", subject=text, body=text
        )
        messages.append({**flag, "hasAttachments": text})
    return messages


@pytest.mark.anyio
async def test_a_hostile_graph_response_leaks_no_corpus_string(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path
) -> None:
    microsoft.messages = hostile_graph_messages()
    # Each fault puts the corpus in Graph's or the authority's status line, headers and body.
    faults = [
        ("graph_fault", "unavailable", "stale"),
        ("graph_fault", "forbidden", "blocked"),
        ("graph_fault", "garbage", "stale"),
        ("token_fault", "consent_withdrawn", "blocked"),
        ("token_fault", "throttled", "stale"),
        ("token_fault", "garbage", "stale"),
        ("token_fault", "wrong_shape", "stale"),
    ]
    results: dict[str, dict[str, Any]] = {}
    with work_gate(start_gate, microsoft) as gate:
        channels = await observe_gate(gate)
        async with agent_client(gate) as client:
            listing = await call(client, LIST_TOOL)
            for attribute, fault, _ in faults:
                setattr(microsoft, attribute, fault)
                results[f"{attribute} {fault}"] = await call(client, LIST_TOOL)
                setattr(microsoft, attribute, None)

    assert len(served(listing)) == len(CORPUS)
    entry = account_of(listing)
    # Twice as many messages as strings cannot be understood; the count is capped at 10.
    assert (entry["status"], entry["skipped_items"]) == ("incomplete", "10+")
    assert [result["error"] for result in results.values()] == ["source_unavailable"] * len(faults)
    assert [account_of(result)["status"] for result in results.values()] == [
        status for _, _, status in faults
    ]
    channels["listing"] = json.dumps(listing)
    channels.update({name: json.dumps(result) for name, result in results.items()})
    channels["log"] = gate.log.read_text(errors="replace")
    assert_no_corpus(channels)
    assert_no_corpus(storage_files(gate.db))


# --- external inference stays off unless the operator enables it -------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("enabled", [False, True])
async def test_external_inference_is_used_for_this_account_only_if_the_operator_lists_it(
    start_gate: GateStarter, microsoft: Microsoft, signed_in: Path, tmp_path: Path, enabled: bool
) -> None:
    state = seed_state(tmp_path / "state", OTHER, TRAVEL)
    listed = ["work"] if enabled else ["someone-else"]
    config = tmp_path / "classifier.json"
    config.write_text(
        json.dumps({"inference": {"external": {"model": "provider-model", "accounts": listed}}})
    )
    with standin("inference-local", state) as local, standin("inference-external", state) as ext:
        env = {
            "GLOVEBOX_CLASSIFIER_CONFIG": str(config),
            "GLOVEBOX_STANDIN_INFERENCE_LOCAL": local.url,
            "GLOVEBOX_STANDIN_INFERENCE_EXTERNAL": ext.url,
        }
        with work_gate(start_gate, microsoft, extra_env=env) as gate:
            async with agent_client(gate) as client:
                await call(client, LIST_TOOL)

    assert (len(local.seen), len(ext.seen)) == ((0, 3) if enabled else (3, 0))


# --- setting the account up -----------------------------------------------------------------


def test_the_login_asks_for_read_access_to_mail_and_stores_it_privately(
    microsoft: Microsoft, tmp_path: Path
) -> None:
    result = operator(
        tmp_path / "login.db",
        *("o365-login", "--client-id", CLIENT_ID, "--authority", microsoft.authority),
        *("--credentials", str(tmp_path / "o365-login")),
    )

    assert result.returncode == 0, result.stderr
    assert "ABCD-EFGH" in result.stderr  # the code the operator is to enter
    stored = tmp_path / "o365-login"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    login = o365.Login.parse(read_credentials(stored))
    assert (login.client_id, login.authority) == (CLIENT_ID, microsoft.authority)
    (device,) = microsoft.device_forms
    # Mail.Read, and what MSAL adds to sign in and to keep the login: nothing that writes.
    assert {scope.lower() for scope in device["scope"].split()} == MAIL_PERMISSIONS
    # No token on the screen.
    assert "access-" not in result.stdout + result.stderr


@pytest.mark.parametrize("fault", ["consent_required", "interaction_required"])
def test_a_sign_in_the_employer_has_not_consented_to_writes_nothing_and_says_why(
    microsoft: Microsoft, tmp_path: Path, fault: str
) -> None:
    microsoft.token_fault = fault
    credentials = tmp_path / "o365-login"

    result = operator(
        tmp_path / "login.db",
        *("o365-login", "--client-id", CLIENT_ID, "--authority", microsoft.authority),
        *("--credentials", str(credentials)),
    )

    assert result.returncode == 1
    assert "AADSTS 65001" in result.stderr and TOKEN_FAULTS[fault][1] in result.stderr
    assert not credentials.exists()
    assert_no_corpus({"stdout": result.stdout, "stderr": result.stderr})


def test_a_sign_in_that_would_grant_write_access_is_refused(
    microsoft: Microsoft, tmp_path: Path
) -> None:
    microsoft.scope = "Mail.ReadWrite Mail.Send openid profile"
    credentials = tmp_path / "o365-login"

    result = operator(
        tmp_path / "login.db",
        *("o365-login", "--client-id", CLIENT_ID, "--authority", microsoft.authority),
        *("--credentials", str(credentials)),
    )

    assert result.returncode == 1
    assert "more than read access" in result.stderr
    assert not credentials.exists()


def test_an_office_365_account_needs_a_login_and_an_https_endpoint(
    microsoft: Microsoft, tmp_path: Path
) -> None:
    db = tmp_path / "glovebox.db"
    login = sign_in(microsoft, tmp_path)
    plain = tmp_path / "not-a-login"
    plain.write_text("just a token")
    plain.chmod(0o600)
    base = ("account", "add", "work", "--kind", O365)

    without = operator(db, *base, "--source", o365.GRAPH_URL)
    plain_http = operator(
        db, *base, "--source", "http://graph.example.test/v1.0", "--credentials", str(login)
    )
    not_a_login = operator(db, *base, "--source", o365.GRAPH_URL, "--credentials", str(plain))
    file_source = operator(
        db, *base, "--source", str(tmp_path / "x.json"), "--credentials", str(login)
    )
    added = operator(db, *base, "--source", o365.GRAPH_URL, "--credentials", str(login))

    assert without.returncode == 1 and "credentials file" in without.stderr
    assert plain_http.returncode == 1 and "https" in plain_http.stderr
    assert not_a_login.returncode == 1 and "glovebox o365-login" in not_a_login.stderr
    assert file_source.returncode == 1
    assert added.returncode == 0, added.stderr
    listed = operator(db, "account", "list").stdout.split("\t")
    assert listed[2:4] == [O365, o365.GRAPH_URL]


# --- below the seam: the adapter's own limits -------------------------------------------------


def adapter(microsoft: Microsoft, credentials: Path) -> o365.O365MailSource:
    return o365.O365MailSource(microsoft.graph, credentials, read_credentials(credentials))


def test_a_window_larger_than_the_cap_is_cut_to_the_newest_and_reported(
    microsoft: Microsoft, signed_in: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(o365, "MAX_MESSAGES", 30)
    microsoft.messages = [
        graph_message(f"m{index:02d}", f"bob@{WORK}", f"2026-01-05T08:{index:02d}:00Z")
        for index in range(40)
    ]

    mailbox = adapter(microsoft, signed_in).fetch(WINDOW_START, NOW)

    assert len(mailbox.items) == 30
    assert mailbox.skipped == 1
    assert {item.upstream_id for item in mailbox.items} == {f"m{n:02d}" for n in range(10, 40)}


def test_a_link_to_another_host_is_never_followed(microsoft: Microsoft, signed_in: Path) -> None:
    microsoft.graph_fault = "foreign_link"
    before = len(microsoft.requests)

    with pytest.raises(SourceError) as raised:
        adapter(microsoft, signed_in).fetch(WINDOW_START, NOW)

    assert not isinstance(raised.value, SourceBlocked)
    # Nothing but the stand-in was asked: the token would have gone to evil.example.test.
    assert all(seen.path.startswith(("/v1.0", "/tenant-0001")) for seen in microsoft.requests)
    assert len(microsoft.graph_requests()) == 1 and len(microsoft.requests) > before


def test_a_graph_endpoint_that_is_not_https_is_not_sent_a_token(
    microsoft: Microsoft, signed_in: Path
) -> None:
    source = o365.O365MailSource("http://127.0.0.1:1/v1.0", signed_in, read_credentials(signed_in))

    with pytest.raises(SourceBlocked):
        source.fetch(WINDOW_START, NOW)

    assert microsoft.requests == []


def test_errors_carry_no_detail_from_upstream(microsoft: Microsoft, signed_in: Path) -> None:
    microsoft.token_fault = "consent_withdrawn"
    with pytest.raises(SourceBlocked) as blocked:
        adapter(microsoft, signed_in).fetch(WINDOW_START, NOW)
    microsoft.token_fault = "unavailable"
    with pytest.raises(SourceError) as outage:
        adapter(microsoft, signed_in).fetch(WINDOW_START, NOW)

    for error in (blocked.value, outage.value):
        assert error.args == ()
        assert error.__cause__ is None
        assert error.__context__ is None or error.__suppress_context__


def test_the_adapter_does_not_print_its_items_text() -> None:
    message = graph_message("a1", f"bob@{WORK}", "2026-01-05T09:00:59Z", subject="S3CRET")
    item = o365.parse_graph_message(message)

    assert item is not None and "S3CRET" in item.text and "S3CRET" not in repr(item)


@pytest.mark.parametrize(
    ("granted", "writes"),
    [
        ("Mail.Read openid profile offline_access email", False),
        ("https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read", False),
        ("Mail.ReadBasic Calendars.Read Mail.Read.Shared Files.Read.All", False),
        ("Mail.Read Mail.ReadWrite", True),
        ("Mail.Send", True),
        ("Mail.Read Directory.AccessAsUser.All", True),
        ("Mail.Read Calendars.ReadWrite", True),
        ("Mail.Read Mail.Read.Write", True),
        ("", False),
    ],
)
def test_which_permissions_could_write(granted: str, writes: bool) -> None:
    assert o365.can_write(o365.granted_scopes(granted)) is writes


def test_a_token_that_names_no_permissions_is_not_shown_to_be_read_only() -> None:
    assert o365.read_only(o365.granted_scopes("Mail.Read openid")) is True
    assert o365.read_only(o365.granted_scopes("Mail.Read Mail.Send")) is False
    assert o365.read_only(o365.granted_scopes("")) is False


def test_a_token_whose_permissions_the_authority_did_not_name_is_refused(
    microsoft: Microsoft, signed_in: Path
) -> None:
    microsoft.scope = ""

    with pytest.raises(SourceBlocked):
        adapter(microsoft, signed_in).fetch(WINDOW_START, NOW)

    assert microsoft.graph_requests() == []


# --- the credentials file ---------------------------------------------------------------------


def test_a_credentials_file_is_replaced_whole_and_stays_private(tmp_path: Path) -> None:
    path = tmp_path / "login"
    path.write_text("old")
    path.chmod(0o600)

    write_credentials(path, "new contents")

    assert path.read_text() == "new contents"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_credentials(path).reveal() == "new contents"
    assert [entry.name for entry in tmp_path.iterdir()] == ["login"]


def test_a_symlinked_credentials_file_stays_a_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.write_text("old")
    real.chmod(0o600)
    link = tmp_path / "link"
    os.symlink(real, link)

    write_credentials(link, "new")

    assert link.is_symlink() and real.read_text() == "new"


def test_a_credentials_file_that_cannot_be_written_is_an_error_without_detail(
    tmp_path: Path,
) -> None:
    with pytest.raises(CredentialsError, match="cannot be written"):
        write_credentials(tmp_path / "missing" / "login", "x")
