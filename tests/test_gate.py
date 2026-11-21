"""Acceptance tests of the gate tracer bullet at the MCP tool-call boundary (ADR 0006).

A real MCP client connects to the running service over Streamable HTTP with the
configured agent token, calls the one read tool, and every channel of every
response is inspected. A few tests reach below the seam on purpose, each marked:
the storage check (retention is a property of the files), and the tampered row
(the only way to make the gate hold a response it must refuse to release).
"""

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx2
import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError

from tests.client import READ_TOOL, agent_client
from tests.conftest import AGENT_TOKEN, GateStarter, RunningGate
from tests.hostile import (
    CORPUS,
    assert_no_corpus,
    expected_hostile_received,
    hostile_mail_fixture,
    leaked_channels,
    observe_gate,
    result_items,
)
from tests.schema import closed_schema_problems
from tests.test_standins import CAST, run_seed, serve

HANDLE = re.compile(r"^[a-z2-7]{24}$")
MINUTE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}Z$")
ITEM_FIELDS = {
    "item_handle",
    "account_handle",
    "source_kind",
    "sender_handle",
    "thread_handle",
    "received",
    "read_status",
    "has_attachments",
}


def write_fixture(path: Path, fixture: dict[str, object]) -> Path:
    path.write_text(json.dumps(fixture))
    return path


def small_fixture() -> dict[str, object]:
    return {
        "account": "alice@example.test",
        "messages": [
            {
                "id": "m1",
                "from": "bob@example.test",
                "thread": "t1",
                "received": "2026-01-05T09:00:59Z",
                "read": True,
                "attachments": [{"filename": "a.pdf"}],
                "subject": "S1",
                "body": "B1",
            },
            {
                "id": "m2",
                "from": "bob@example.test",
                "thread": "t1",
                "received": "2026-01-05T10:30:00+00:00",
                "read": False,
                "subject": "S2",
                "body": "B2",
            },
            {
                "id": "m3",
                "from": "carol@example.test",
                "received": "2026-01-06T08:00:00Z",
                "subject": "S3",
                "body": "B3",
            },
        ],
    }


async def read_items(gate: RunningGate) -> list[dict[str, object]]:
    async with agent_client(gate) as client:
        result = await client.call_tool(READ_TOOL)
    assert result.is_error is False
    return result_items(result)


def storage_files(db: Path) -> dict[str, bytes]:
    """Every file SQLite keeps for the database: the file itself and any journal."""
    return {path.name: path.read_bytes() for path in db.parent.glob(db.name + "*")}


@contextmanager
def counting_source(fixture: dict[str, object]) -> Iterator[tuple[str, list[str]]]:
    """An HTTP mail source that records each request it receives."""
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requests.append(self.path)
            body = json.dumps(fixture).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()


# --- authentication ---------------------------------------------------------------------


def test_requests_without_a_valid_token_are_rejected_before_any_tool_runs(
    start_gate: GateStarter,
) -> None:
    call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": READ_TOOL}}
    accept = {"Accept": "application/json, text/event-stream"}
    wrong = [
        {},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer wrong-token-0123456789abcdef"},
        {"Authorization": f"Bearer {AGENT_TOKEN}x"},
        {"Authorization": f"Bearer {AGENT_TOKEN[:-1]}"},
        {"Authorization": f"Basic {AGENT_TOKEN}"},
        {"Authorization": AGENT_TOKEN},
        {"X-Api-Key": AGENT_TOKEN},
    ]
    with (
        counting_source(small_fixture()) as (source_url, source_requests),
        start_gate(mail_url=source_url) as gate,
        httpx2.Client(timeout=30) as http,
    ):
        for headers in wrong:
            posted = http.post(gate.url, json=call, headers={**accept, **headers})
            fetched = http.get(gate.url, headers={**accept, **headers})
            assert posted.status_code == 401
            assert fetched.status_code == 401
        # A path the gate does not serve is refused the same way, not with a 404.
        assert http.get(gate.url.replace("/mcp", "/other")).status_code == 401
        assert source_requests == []

        authorised = http.post(
            gate.url, json=call, headers={**accept, "Authorization": f"Bearer {AGENT_TOKEN}"}
        )
        assert authorised.status_code != 401


@pytest.mark.anyio
async def test_a_client_with_the_wrong_token_cannot_connect(gate: RunningGate) -> None:
    with pytest.raises(Exception):  # noqa: B017 - the client's error type is not the contract
        async with agent_client(gate, token="wrong-token-0123456789abcdef") as client:
            await client.list_tools()


@pytest.mark.anyio
async def test_a_client_that_sends_no_token_cannot_connect(gate: RunningGate) -> None:
    with pytest.raises(Exception):  # noqa: B017 - the client's error type is not the contract
        async with Client(gate.url) as client:
            await client.list_tools()


def test_the_service_refuses_to_start_without_a_usable_token(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GLOVEBOX_")}
    env["GLOVEBOX_DB"] = str(tmp_path / "glovebox.db")
    for token in (None, "", "short", " " * 32):
        attempt = env if token is None else {**env, "GLOVEBOX_AGENT_TOKEN": token}
        result = subprocess.run(
            [sys.executable, "-m", "glovebox"],
            env=attempt,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode != 0


# --- the tool surface -------------------------------------------------------------------


@pytest.mark.anyio
async def test_only_the_read_tool_is_offered_with_a_closed_output_schema(gate: RunningGate) -> None:
    async with agent_client(gate) as client:
        tools = (await client.list_tools()).tools
        capabilities = client.server_capabilities
        with pytest.raises(MCPError):
            await client.list_resources()
        with pytest.raises(MCPError):
            await client.list_prompts()

    assert [tool.name for tool in tools] == [READ_TOOL]
    assert capabilities.resources is None
    assert capabilities.prompts is None
    assert capabilities.tools is not None
    tool = tools[0]
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.output_schema is not None
    assert closed_schema_problems(tool.output_schema) == []
    assert closed_schema_problems(tool.input_schema) == []
    assert tool.input_schema["properties"] == {}


def test_the_schema_check_finds_open_strings_and_objects() -> None:
    open_string = {
        "type": "object",
        "properties": {"s": {"type": "string"}},
        "additionalProperties": False,
    }
    open_object = {
        "type": "object",
        "properties": {"o": {"type": "object"}},
        "additionalProperties": False,
    }
    extra_properties = {"type": "object", "properties": {"b": {"type": "boolean"}}}

    assert closed_schema_problems(open_string)
    assert closed_schema_problems(open_object)
    assert closed_schema_problems(extra_properties)


@pytest.mark.anyio
async def test_the_read_tool_returns_only_structured_permitted_fields(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    fixture = write_fixture(tmp_path / "mail.json", small_fixture())
    with start_gate(mail_fixture=fixture) as gate:
        async with agent_client(gate) as client:
            result = await client.call_tool(READ_TOOL)

    assert result.content == []
    assert result.is_error is False
    # The SDK stamps the server's own name and version on every result; nothing else.
    assert set(result.meta or {}) <= {"io.modelcontextprotocol/serverInfo"}
    structured = result.structured_content
    assert isinstance(structured, dict)
    assert set(structured) == {"error", "items"}
    assert structured["error"] == "none"
    items = result_items(result)
    # Newest first.
    assert [item["received"] for item in items] == [
        "2026-01-06T08:00Z",
        "2026-01-05T10:30Z",
        "2026-01-05T09:00Z",
    ]
    for item in items:
        assert set(item) == ITEM_FIELDS
        assert item["source_kind"] == "fixture_mail"
        assert HANDLE.match(str(item["item_handle"]))
        assert HANDLE.match(str(item["account_handle"]))
        assert HANDLE.match(str(item["sender_handle"]))
        assert HANDLE.match(str(item["thread_handle"]))
        assert MINUTE.match(str(item["received"]))
    carol, bob_second, bob_first = items
    assert [item["read_status"] for item in items] == ["unknown", "unread", "read"]
    assert [item["has_attachments"] for item in items] == [False, False, True]
    # The same sender and thread share handles; entities of different kinds do not.
    assert bob_first["sender_handle"] == bob_second["sender_handle"]
    assert bob_first["sender_handle"] != carol["sender_handle"]
    assert bob_first["thread_handle"] == bob_second["thread_handle"]
    assert bob_first["thread_handle"] != carol["thread_handle"]
    assert len({item["account_handle"] for item in items}) == 1
    all_handles = [
        handle
        for item in items
        for handle in (item["item_handle"], item["sender_handle"], item["thread_handle"])
    ]
    # Nine slots; only bob's sender and thread are each shared by two items.
    assert len(set(all_handles)) == 7


@pytest.mark.anyio
async def test_a_call_with_arguments_or_an_unknown_tool_gets_a_fixed_error(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    fixture = write_fixture(tmp_path / "mail.json", small_fixture())
    with start_gate(mail_fixture=fixture) as gate:
        async with agent_client(gate) as client:
            with_arguments = await client.call_tool(READ_TOOL, {"query": "anything"})
            unknown = await client.call_tool("send_email", {})

    for result, error in ((with_arguments, "invalid_arguments"), (unknown, "unknown_tool")):
        assert result.is_error is True
        assert result.content == []
        assert result.structured_content == {"error": error, "items": []}


@pytest.mark.anyio
async def test_a_source_that_cannot_be_read_is_a_fixed_error_not_an_empty_inbox(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    missing = tmp_path / "missing.json"
    broken = tmp_path / "broken.json"
    broken.write_text(CORPUS["injection"])
    for gate_options in ({}, {"mail_fixture": missing}, {"mail_fixture": broken}):
        with start_gate(**gate_options) as gate:
            async with agent_client(gate) as client:
                result = await client.call_tool(READ_TOOL)
            assert result.is_error is True
            assert result.content == []
            assert result.structured_content == {"error": "source_unavailable", "items": []}
            assert_no_corpus({"result": result.model_dump_json(by_alias=True)})


# --- handles ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_only_the_configured_account_is_served_when_the_source_changes(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    """A database that already holds another account's items must not serve them."""
    first = write_fixture(tmp_path / "first.json", small_fixture())
    other = small_fixture()
    other["account"] = "dave@example.test"
    other["messages"] = [
        {
            "id": "d1",
            "from": "erin@example.test",
            "received": "2026-02-01T12:00:00Z",
            "read": False,
        }
    ]
    second = write_fixture(tmp_path / "second.json", other)
    with start_gate(mail_fixture=first) as gate:
        before = await read_items(gate)
    with start_gate(mail_fixture=second) as gate:
        after = await read_items(gate)
    with start_gate(mail_fixture=first) as gate:
        back = await read_items(gate)

    assert len(before) == 3
    assert [item["received"] for item in after] == ["2026-02-01T12:00Z"]
    assert after[0]["account_handle"] != before[0]["account_handle"]
    assert back == before


@pytest.mark.anyio
async def test_handles_are_stable_across_calls_and_restarts_and_unrelated_across_databases(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    fixture = write_fixture(tmp_path / "mail.json", small_fixture())
    with start_gate("one.db", mail_fixture=fixture) as gate:
        first = await read_items(gate)
        second = await read_items(gate)
    with start_gate("one.db", mail_fixture=fixture) as gate:
        after_restart = await read_items(gate)
    with start_gate("two.db", mail_fixture=fixture) as gate:
        elsewhere = await read_items(gate)

    assert first == second == after_restart
    handles = {key: {str(item[key]) for item in first} for key in ("item_handle", "sender_handle")}
    other = {key: {str(item[key]) for item in elsewhere} for key in handles}
    for key in handles:
        assert handles[key].isdisjoint(other[key]), "a handle is derived from the content"


# --- the hostile corpus -----------------------------------------------------------------


def test_the_corpus_assertion_detects_a_fragment_in_any_form() -> None:
    text = CORPUS["injection"]
    assert leaked_channels({"plain": f"prefix {text} suffix"}) == ["plain"]
    assert leaked_channels({"fragment": text[10:24]}) == ["fragment"]
    assert leaked_channels({"upper": text.upper()}) == ["upper"]
    assert leaked_channels({"escaped": json.dumps(CORPUS["newlines"])}) == ["escaped"]
    assert leaked_channels({"bytes": CORPUS["unicode"].encode()}) == ["bytes"]
    assert leaked_channels({"clean": '{"error": "none", "items": []}'}) == []


@pytest.mark.anyio
async def test_no_corpus_string_reaches_any_channel(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    fixture = write_fixture(tmp_path / "hostile.json", hostile_mail_fixture())
    with start_gate(mail_fixture=fixture) as gate:
        channels = await observe_gate(gate)
        items = await read_items(gate)

    assert [item["received"] for item in items] == expected_hostile_received()
    assert {"call", "tools", "refusal", "bad-arguments", "unknown-tool"} <= set(channels)
    assert_no_corpus(channels)
    # Values that name no permitted status are `unknown`, whatever the source put there.
    assert {item["read_status"] for item in items} <= {"read", "unread", "unknown"}


@pytest.mark.anyio
async def test_raw_text_is_not_persisted(start_gate: GateStarter, tmp_path: Path) -> None:
    """Below the seam on purpose: retention is a property of the stored files."""
    fixture = write_fixture(tmp_path / "hostile.json", hostile_mail_fixture())
    with start_gate(mail_fixture=fixture) as gate:
        await read_items(gate)

    files = storage_files(gate.db)

    assert gate.db.name in files
    assert_no_corpus(files)
    assert b"evil.test" not in b"".join(files.values())
    # What is kept is the permitted fields, handle mappings and sync state.
    with sqlite3.connect(gate.db) as database:
        tables = {
            row[0] for row in database.execute("select name from sqlite_master where type='table'")
        }
        item_count = database.execute("select count(*) from items").fetchone()[0]
    assert item_count == len(CORPUS)
    assert {"items", "handles", "sync_state"} <= tables


# --- the output validator ---------------------------------------------------------------


@pytest.mark.anyio
async def test_a_response_that_fails_validation_is_replaced_by_the_fixed_error(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    """Below the seam on purpose: a stored row is made invalid so that the gate holds a
    response it must refuse to release. The trigger corrupts every row as it is written."""
    fixture = write_fixture(tmp_path / "mail.json", small_fixture())
    with start_gate(mail_fixture=fixture) as gate:
        with sqlite3.connect(gate.db) as database:
            for event in ("INSERT", "UPDATE"):
                database.execute(
                    f"create trigger tamper_{event.lower()} after {event} on items begin "
                    f"update items set read_status = '{CORPUS['injection']}' "
                    "where item_handle = new.item_handle; end"
                )
        async with agent_client(gate) as client:
            result = await client.call_tool(READ_TOOL)

    assert result.is_error is True
    assert result.content == []
    assert result.structured_content == {"error": "response_invalid", "items": []}
    assert_no_corpus({"result": result.model_dump_json(by_alias=True)})


# --- the throwaway fixture cast ---------------------------------------------------------


@pytest.mark.anyio
async def test_the_fixture_cast_is_served_through_the_running_gate(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    state = tmp_path / "seed"
    assert run_seed(state).returncode == 0
    cast = json.loads(CAST.read_text())["services"]["mailbox-mail"]
    with serve("mailbox-mail", state) as source_url, start_gate(mail_url=source_url) as gate:
        channels = await observe_gate(gate)
        items = await read_items(gate)

    assert [item["received"] for item in items] == [
        "2026-01-06T08:00Z",
        "2026-01-05T10:30Z",
        "2026-01-05T09:00Z",
    ]
    assert len({item["sender_handle"] for item in items}) == 3
    assert {item["read_status"] for item in items} == {"unknown"}
    # Nothing the stand-in served is visible: not an address, a subject, a body or an ID.
    served = [cast["account"]]
    for message in cast["messages"]:
        served += [message["id"], message["from"], message["subject"], message["body"]]
    everything: dict[str, str | bytes] = {**channels, **storage_files(gate.db)}
    for name, content in everything.items():
        text = content if isinstance(content, str) else content.decode("utf-8", "replace")
        assert not any(value in text for value in served), name
    assert_no_corpus(everything)


@pytest.mark.anyio
async def test_an_unseeded_stand_in_is_a_fixed_error(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with serve("mailbox-mail", tmp_path / "unseeded") as source_url:
        with start_gate(mail_url=source_url) as gate:
            async with agent_client(gate) as client:
                result = await client.call_tool(READ_TOOL)

    assert result.is_error is True
    assert result.structured_content == {"error": "source_unavailable", "items": []}
