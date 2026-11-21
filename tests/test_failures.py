"""Acceptance tests of source outages and sync freshness at the MCP tool-call boundary (ADR 0006).

A real MCP client calls the item, count and group tools of a running gate whose one mail
source is an HTTP fixture that a test can break and mend. Every fault is hostile: the
status line, headers and body the source answers with are full of corpus strings, and
the assertion is that none of them reaches a result or the gate's log. One fault at a
time is injected; simultaneous faults are out of scope. Two tests reach below the seam
on purpose, each marked: a tampered sync row (the only way to make the gate hold an
invalid response) and a file the gate may not read.
"""

import json
import os
import socket
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anyio
import pytest

from tests.client import COUNT_TOOL, GROUP_TOOL, LIST_TOOL, agent_client
from tests.conftest import GateStarter, RunningGate
from tests.hostile import CORPUS, OK_CALLS, assert_no_corpus, hostile_mail_fixture, observe_gate
from tests.test_gate import HANDLE, MINUTE, small_fixture, write_fixture
from tests.test_queries import call

FIRST_SYNC = "2026-01-10T12:00:00Z"
LATER_SYNC = "2026-01-10T12:30:00Z"
SYNC_FIELDS = {"account_handle", "source_kind", "status", "last_sync", "skipped_items"}

HOSTILE_BODY = json.dumps(
    {
        "error": list(CORPUS.values()),
        "detail": CORPUS["injection"],
        "messages": CORPUS["json_break"],
    }
).encode()

# Every way the fixture source can fail, and the status an agent is told. One at a time.
OUTAGES = ["unavailable", "reset", "garbage", "wrong_shape", "empty", "redirect"]
BLOCKS = ["unauthorized", "forbidden"]


@dataclass
class Source:
    """An HTTP mail source whose answer a test changes between calls."""

    fixture: dict[str, object]
    fault: str | None = None
    delay: float = 0.0
    url: str = ""
    requests: list[str] = field(default_factory=list)


def _respond(handler: BaseHTTPRequestHandler, status: int, reason: str, body: bytes) -> None:
    handler.send_response(status, reason)
    handler.send_header("Content-Type", "text/plain")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("X-Detail", CORPUS["injection"])
    handler.send_header("WWW-Authenticate", f'Basic realm="{CORPUS["marker"]}"')
    handler.end_headers()
    handler.wfile.write(body)


@contextmanager
def broken_source(fixture: dict[str, object]) -> Iterator[Source]:
    source = Source(fixture)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            source.requests.append(self.path)
            time.sleep(source.delay)
            fault = source.fault
            try:
                if fault is None:
                    _respond(self, 200, "OK", json.dumps(source.fixture).encode())
                elif fault == "unavailable":
                    _respond(self, 503, CORPUS["marker"], HOSTILE_BODY)
                elif fault == "unauthorized":
                    _respond(self, 401, CORPUS["marker"], HOSTILE_BODY)
                elif fault == "forbidden":
                    _respond(self, 403, CORPUS["marker"], HOSTILE_BODY)
                elif fault == "garbage":
                    _respond(self, 200, "OK", "\n".join(CORPUS.values()).encode())
                elif fault == "wrong_shape":
                    _respond(self, 200, "OK", HOSTILE_BODY)
                elif fault == "empty":
                    _respond(self, 200, "OK", b"")
                elif fault == "redirect":
                    self.send_response(302)
                    self.send_header("Location", CORPUS["injection"])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                # "reset": the connection closes without an answer.
            except (BrokenPipeError, ConnectionResetError):
                pass  # the gate gave up on this request

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    source.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield source
    finally:
        server.shutdown()
        server.server_close()


def account_of(structured: dict[str, Any]) -> dict[str, Any]:
    accounts = structured["accounts"]
    assert len(accounts) == 1
    account: dict[str, Any] = accounts[0]
    assert set(account) == SYNC_FIELDS
    assert HANDLE.match(account["account_handle"])
    assert MINUTE.match(account["last_sync"])
    return account


def assert_clean(gate: RunningGate, *results: dict[str, Any]) -> None:
    """No corpus string in the results or in anything the gate logged."""
    channels = {f"result {index}": json.dumps(result) for index, result in enumerate(results)}
    channels["log"] = gate.log.read_text(errors="replace")
    assert_no_corpus(channels)


# --- source outages ---------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("fault", OUTAGES)
async def test_an_outage_is_the_fixed_error_with_stale_status_and_ends_without_operator_action(
    start_gate: GateStarter, fault: str
) -> None:
    with broken_source(small_fixture()) as source, start_gate(mail_url=source.url) as gate:
        async with agent_client(gate) as client:
            before = await call(client, LIST_TOOL)
            current = account_of(before)
            assert current["status"] == "current"
            assert current["source_kind"] == "fixture_mail"
            assert current["last_sync"] == "2026-01-10T12:00Z"
            assert current["skipped_items"] == "0"
            assert len(before["items"]) == 3
            counted_before = await call(client, COUNT_TOOL)
            assert counted_before["count"] == "3"
            assert account_of(counted_before) == current

            source.fault = fault
            outage = {tool: await call(client, tool, **OK_CALLS[tool]) for tool in OK_CALLS}
            for tool, result in outage.items():
                # Both hold at once: the fixed outage error, and the account's freshness.
                assert result["error"] == "source_unavailable", tool
                assert account_of(result) == {**current, "status": "stale"}, tool
            # An outage is never an apparently complete empty result.
            assert outage[LIST_TOOL]["items"] == []
            assert outage[COUNT_TOOL]["count"] == "0"
            assert outage[GROUP_TOOL]["groups"] == []

            source.fault = None
            after = await call(client, LIST_TOOL)
            assert after == before
            counted_after = await call(client, COUNT_TOOL)
            assert counted_after["count"] == "3"
            assert account_of(counted_after) == current

        log = gate.log.read_text(errors="replace")
        assert log.count("source sync failed: stale") == 3
        assert_clean(gate, *outage.values(), before, after)


@pytest.mark.anyio
async def test_an_outage_before_any_sync_is_the_fixed_error_and_names_no_account(
    start_gate: GateStarter,
) -> None:
    with broken_source(small_fixture()) as source:
        source.fault = "unavailable"
        with start_gate(mail_url=source.url) as gate:
            async with agent_client(gate) as client:
                down = await call(client, COUNT_TOOL)
                source.fault = None
                up = await call(client, COUNT_TOOL)

    assert down == {"error": "source_unavailable", "count": "0", "accounts": []}
    assert up["error"] == "none"
    assert up["count"] == "3"
    assert account_of(up)["status"] == "current"


@pytest.mark.anyio
async def test_an_outage_survives_a_restart_and_the_last_sync_time_is_the_last_success(
    start_gate: GateStarter,
) -> None:
    with broken_source(small_fixture()) as source:
        with start_gate(mail_url=source.url, now=FIRST_SYNC) as gate:
            async with agent_client(gate) as client:
                first = account_of(await call(client, LIST_TOOL))
        source.fault = "unavailable"
        with start_gate(mail_url=source.url, now=LATER_SYNC) as gate:
            async with agent_client(gate) as client:
                down = await call(client, COUNT_TOOL)
                source.fault = None
                up = await call(client, COUNT_TOOL)

    assert first["last_sync"] == "2026-01-10T12:00Z"
    assert down["error"] == "source_unavailable"
    assert account_of(down) == {**first, "status": "stale"}
    assert up["error"] == "none"
    assert account_of(up) == {**first, "status": "current", "last_sync": "2026-01-10T12:30Z"}


# --- blocked access ---------------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize("fault", BLOCKS)
async def test_a_source_that_refuses_access_is_blocked_and_recovers_when_access_returns(
    start_gate: GateStarter, fault: str
) -> None:
    with broken_source(small_fixture()) as source, start_gate(mail_url=source.url) as gate:
        async with agent_client(gate) as client:
            current = account_of(await call(client, LIST_TOOL))
            source.fault = fault
            blocked = {tool: await call(client, tool, **OK_CALLS[tool]) for tool in OK_CALLS}
            source.fault = None
            after = await call(client, COUNT_TOOL)

        for tool, result in blocked.items():
            assert result["error"] == "source_unavailable", tool
            assert account_of(result) == {**current, "status": "blocked"}, tool
        assert account_of(after) == current
        assert gate.log.read_text(errors="replace").count("source sync failed: blocked") == 3
        assert_clean(gate, *blocked.values(), after)


@pytest.mark.anyio
@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
async def test_a_fixture_file_the_gate_may_not_read_is_blocked(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    """Below the seam on purpose: the file's mode is how access is withdrawn."""
    path = write_fixture(tmp_path / "mail.json", small_fixture())
    with start_gate(mail_fixture=path) as gate:
        async with agent_client(gate) as client:
            current = account_of(await call(client, LIST_TOOL))
            path.chmod(0)
            try:
                blocked = await call(client, COUNT_TOOL)
            finally:
                path.chmod(0o600)
            after = await call(client, COUNT_TOOL)

    assert blocked["error"] == "source_unavailable"
    assert account_of(blocked) == {**current, "status": "blocked"}
    assert account_of(after) == current


# --- unparseable items ------------------------------------------------------------------


def with_unparseable(count: int) -> dict[str, object]:
    """The small fixture plus `count` items that cannot be understood, in different ways."""
    fixture = small_fixture()
    bad: list[object] = [
        "not an object",
        7,
        None,
        ["nested"],
        {"from": "bob@example.test", "received": "2026-01-05T09:00:00Z"},
        {"id": "x1", "received": "2026-01-05T09:00:00Z"},
        {"id": "x2", "from": "bob@example.test", "received": CORPUS["injection"]},
        {"id": "x3", "from": "bob@example.test", "received": "2026-01-05T09:00:00"},
        {"id": 4, "from": "bob@example.test", "received": "2026-01-05T09:00:00Z"},
        {"id": "x5", "from": 5, "received": "2026-01-05T09:00:00Z"},
    ]
    messages = fixture["messages"]
    assert isinstance(messages, list)
    messages += [bad[index % len(bad)] for index in range(count)]
    return fixture


@pytest.mark.anyio
async def test_unparseable_items_are_skipped_and_only_a_status_and_a_capped_count_show(
    start_gate: GateStarter,
) -> None:
    with broken_source(with_unparseable(6)) as source, start_gate(mail_url=source.url) as gate:
        async with agent_client(gate) as client:
            listed = await call(client, LIST_TOOL)
            counted = await call(client, COUNT_TOOL)
            grouped = await call(client, GROUP_TOOL, group_by="sender_handle")
            skipped = account_of(listed)
            assert (skipped["status"], skipped["skipped_items"]) == ("incomplete", "6")
            # The rest of the response is as if the bad items were not there.
            assert listed["error"] == counted["error"] == grouped["error"] == "none"
            assert len(listed["items"]) == 3
            assert counted["count"] == "3"
            assert sum(int(group["count"]) for group in grouped["groups"]) == 3
            assert account_of(counted) == account_of(grouped) == skipped

            source.fixture = with_unparseable(25)
            capped = account_of(await call(client, COUNT_TOOL))
            assert (capped["status"], capped["skipped_items"]) == ("incomplete", "10+")

            source.fixture = small_fixture()
            clean = await call(client, LIST_TOOL)
            assert clean["items"] == listed["items"]
            fixed = account_of(clean)
            assert (fixed["status"], fixed["skipped_items"]) == ("current", "0")
        assert_clean(gate, listed, counted, grouped, clean)


@pytest.mark.anyio
async def test_hostile_unparseable_items_reach_no_channel_and_log(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    fixture = write_fixture(tmp_path / "hostile.json", hostile_mail_fixture())
    with start_gate(mail_fixture=fixture) as gate:
        async with agent_client(gate) as client:
            counted = await call(client, COUNT_TOOL)
        channels = await observe_gate(gate)

    assert counted["count"] == str(len(CORPUS))
    sync = account_of(counted)
    assert (sync["status"], sync["skipped_items"]) == ("incomplete", "10+")
    assert "log" in channels
    assert_no_corpus({**channels, "result": json.dumps(counted)})


# --- fail-closed validation -------------------------------------------------------------


@pytest.mark.anyio
async def test_a_sync_status_that_fails_validation_is_replaced_by_the_fixed_error(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    """Below the seam on purpose: a stored sync row is made invalid, so the gate holds a
    response it must refuse to release. The trigger corrupts the row as it is written."""
    fixture = write_fixture(tmp_path / "mail.json", small_fixture())
    with start_gate(mail_fixture=fixture) as gate:
        with sqlite3.connect(gate.db) as database:
            for event in ("INSERT", "UPDATE"):
                database.execute(
                    f"create trigger tamper_{event.lower()} after {event} on sync_state begin "
                    f"update sync_state set status = '{CORPUS['injection']}' "
                    "where account_handle = new.account_handle; end"
                )
        async with agent_client(gate) as client:
            results = {tool: await call(client, tool, **OK_CALLS[tool]) for tool in OK_CALLS}

    # Nothing of the failed response survives, not even the account that was read.
    assert results[LIST_TOOL] == {
        "error": "response_invalid",
        "accounts": [],
        "items": [],
        "next_cursor": None,
    }
    assert results[COUNT_TOOL] == {"error": "response_invalid", "accounts": [], "count": "0"}
    assert results[GROUP_TOOL] == {
        "error": "response_invalid",
        "accounts": [],
        "groups": [],
        "next_cursor": None,
    }
    assert_clean(gate, *results.values())


# --- client cancellation ----------------------------------------------------------------


@pytest.mark.anyio
async def test_a_cancelled_call_leaves_the_sync_state_intact(start_gate: GateStarter) -> None:
    with broken_source(small_fixture()) as source, start_gate(mail_url=source.url) as gate:
        async with agent_client(gate) as client:
            before = await call(client, LIST_TOOL)

        source.delay = 0.8
        async with agent_client(gate) as client:
            with anyio.move_on_after(0.2) as scope:
                await client.call_tool(LIST_TOOL)
            assert scope.cancelled_caught

        source.delay = 0
        async with agent_client(gate) as client:
            after = await call(client, LIST_TOOL)
            source.fault = "unavailable"
            down = await call(client, COUNT_TOOL)

    assert after == before
    assert account_of(down) == {**account_of(before), "status": "stale"}
    log = gate.log.read_text(errors="replace")
    assert "Traceback" not in log
    assert_clean(gate, before, after, down)


@pytest.mark.anyio
async def test_a_database_from_before_sync_counts_is_upgraded_in_place(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    """Below the seam on purpose: the old database is made by hand, as the earlier schema."""
    with sqlite3.connect(tmp_path / "glovebox.db") as database:
        database.executescript(
            "create table sync_state (account_handle text primary key, source_kind text not null,"
            " status text not null, last_sync text not null);"
            "insert into sync_state values ('abcdefghijklmnopqrstuvwx', 'fixture_mail',"
            " 'current', '2025-12-01T00:00Z');"
        )
    fixture = write_fixture(tmp_path / "mail.json", small_fixture())
    with start_gate(mail_fixture=fixture) as gate:
        async with agent_client(gate) as client:
            result = await call(client, COUNT_TOOL)

    assert result["count"] == "3"
    assert account_of(result)["skipped_items"] == "0"


@pytest.mark.anyio
async def test_a_source_that_is_not_listening_is_an_outage_not_an_empty_inbox(
    start_gate: GateStarter,
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed = f"http://127.0.0.1:{probe.getsockname()[1]}"
    with start_gate(mail_url=closed) as gate:
        async with agent_client(gate) as client:
            result = await call(client, LIST_TOOL)

    assert result == {
        "error": "source_unavailable",
        "items": [],
        "next_cursor": None,
        "accounts": [],
    }
