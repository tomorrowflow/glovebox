"""Acceptance tests of the read-only iMessage source (ADR 0003, 0004, 0006, 0007).

Everything goes through MCP tool calls from a real client against a running gate. An
`imessage` account reads either the iMessage stand-in over HTTP (the bridge, as the test tier
runs it) or a Messages database file (`chat.db`) directly. The HTTP tests use the real
stand-in seeded from the fixture cast, and a recording bridge that logs every request it gets
so that "the gate calls no write operation" is a fact about requests, not a promise. The
database tests build a database file with the table layout Messages uses and fictional
content; what a real Messages database holds is checked by the operator (docs/imessage.md).
Tests that reach below the seam say so: what is stored or left on disk is not visible
through a tool call. One fault at a time; simultaneous faults are out of scope.
"""

import json
import os
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import anyio
import pytest

from glovebox import chatdb
from tests.client import ACCOUNTS_TOOL, COUNT_TOOL, GROUP_TOOL, LIST_TOOL, agent_client
from tests.conftest import GateStarter, RunningGate, operator
from tests.hostile import CORPUS, assert_no_corpus, observe_gate
from tests.test_failures import HOSTILE_BODY, account_of
from tests.test_gate import storage_files
from tests.test_messenger import conversation, messenger, say
from tests.test_queries import call
from tests.test_standins import run_seed, serve

IMESSAGE = "imessage"
OPERATOR_ID = "alice@example.test"
BOB = "bob@example.test"
CAROL = "+15550102"
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
FIRST_SYNC = "2026-01-10T12:00Z"


# --- a recording bridge over HTTP -------------------------------------------------------


@dataclass
class Bridge:
    """A bridge stand-in that records every request, whatever its method, and can fail."""

    document: dict[str, object]
    token: str | None = None  # the bearer token it demands, if any
    fault: str | None = None
    delay: float = 0.0
    url: str = ""
    requests: list[tuple[str, str, str | None]] = field(default_factory=list)


@contextmanager
def bridge_serving(document: dict[str, object], token: str | None = None) -> Iterator[Bridge]:
    bridge = Bridge(document, token)

    class Handler(BaseHTTPRequestHandler):
        def _answer(self, status: int, body: bytes = b"") -> None:
            self.send_response(status, CORPUS["marker"] if status >= 400 else "OK")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Detail", CORPUS["injection"])
            self.end_headers()
            self.wfile.write(body)

        def _handle(self) -> None:
            bridge.requests.append((self.command, self.path, self.headers.get("Authorization")))
            fault = bridge.fault
            threading.Event().wait(bridge.delay)
            try:
                if self.command != "GET":
                    self._answer(405, HOSTILE_BODY)
                elif bridge.token and self.headers.get("Authorization") != f"Bearer {bridge.token}":
                    self._answer(401, HOSTILE_BODY)
                elif fault == "unavailable":
                    self._answer(503, HOSTILE_BODY)
                elif fault == "forbidden":
                    self._answer(403, HOSTILE_BODY)
                elif fault == "garbage":
                    self._answer(200, "\n".join(CORPUS.values()).encode())
                else:
                    self._answer(200, json.dumps(bridge.document).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass  # the gate gave up on this request

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    bridge.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield bridge
    finally:
        server.shutdown()
        server.server_close()


def triage() -> dict[str, object]:
    """Two direct conversations and a group; four messages in the window and one older."""
    return messenger(
        conversation(
            "dm-bob",
            [OPERATOR_ID, BOB],
            say("a1", BOB, "2026-01-05T18:00:00Z", read=True),
            say("a2", BOB, "2026-01-05T18:05:30Z", read=False),
            say("old", BOB, "2025-11-01T18:00:00Z", read=False),
        ),
        conversation(
            "dm-carol",
            [OPERATOR_ID, CAROL],
            say("b1", CAROL, "2026-01-06T07:10:00Z", read=False),
        ),
        conversation(
            "club",
            [OPERATOR_ID, BOB, CAROL],
            say("c1", BOB, "2026-01-07T20:00:00Z", read=True, media=[{"type": "image/png"}]),
            group=True,
        ),
        account=OPERATOR_ID,
    )


def served(structured: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = structured["items"]
    return items


# --- the stand-in over HTTP: mapping and the read-only report ----------------------------


@pytest.mark.anyio
async def test_the_imessage_stand_in_is_served_as_messenger_items(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    state = tmp_path / "seed"
    assert run_seed(state).returncode == 0
    cast = json.loads((state / "imessage.json").read_text())
    with serve("imessage", state) as url:
        with start_gate(accounts={"chat": url}, kinds={"chat": IMESSAGE}) as gate:
            async with agent_client(gate) as client:
                listing = await call(client, LIST_TOOL)
                tools = (await client.list_tools()).model_dump_json()
            channels = {"listing": json.dumps(listing), "tools": tools}
            channels["log"] = gate.log.read_text(errors="replace")

    (item,) = served(listing)
    assert item["source_kind"] == IMESSAGE
    assert item["received"] == "2026-01-05T19:20Z"
    assert item["read_status"] == "unknown"
    assert item["has_attachments"] is False
    account = account_of(listing)
    assert (account["status"], account["source_kind"]) == ("current", IMESSAGE)
    # Nothing the stand-in served crosses the gate or is stored.
    chat = cast["conversations"][0]
    values = [cast["account"], chat["id"], *chat["participants"], chat["messages"][0]["text"]]
    everything: dict[str, str | bytes] = {**channels, **storage_files(gate.db)}
    for name, content in everything.items():
        text = content if isinstance(content, str) else content.decode("utf-8", "replace")
        assert not any(value in text for value in values), name


@pytest.mark.anyio
async def test_messages_are_mapped_to_the_messenger_item_fields(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with bridge_serving(triage()) as bridge:
        with start_gate(accounts={"chat": bridge.url}, kinds={"chat": IMESSAGE}) as gate:
            async with agent_client(gate) as client:
                listing = await call(client, LIST_TOOL, sort="received_asc")
                threads = await call(client, GROUP_TOOL, group_by="thread_handle")
                senders = await call(client, GROUP_TOOL, group_by="sender_handle")
                unread = await call(client, COUNT_TOOL, read_status="unread")

    items = served(listing)
    # The message from before the window is neither ingested nor served.
    assert [item["received"] for item in items] == [
        "2026-01-05T18:00Z",
        "2026-01-05T18:05Z",
        "2026-01-06T07:10Z",
        "2026-01-07T20:00Z",
    ]
    assert [item["read_status"] for item in items] == ["read", "unread", "unread", "read"]
    assert [item["has_attachments"] for item in items] == [False, False, False, True]
    assert {item["source_kind"] for item in items} == {IMESSAGE}
    # Direct and group conversations are three conversations; two people are two senders.
    assert sorted(group["count"] for group in threads["groups"]) == ["1", "1", "2"]
    assert sorted(group["count"] for group in senders["groups"]) == ["1", "3"]
    assert unread["count"] == "2"
    assert account_of(listing)["last_sync"] == FIRST_SYNC


@pytest.mark.anyio
async def test_an_account_reports_that_read_only_is_enforced_by_the_gateway(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with bridge_serving(triage()) as bridge:
        with start_gate(accounts={"chat": bridge.url}, kinds={"chat": IMESSAGE}) as gate:
            async with agent_client(gate) as client:
                accounts = await call(client, ACCOUNTS_TOOL)
                listing = await call(client, LIST_TOOL)

    for result in (accounts, listing):
        (entry,) = result["accounts"]
        assert entry["read_only_enforcement"] == "gateway"


# --- the gate calls nothing that changes state ---------------------------------------------


@pytest.mark.anyio
async def test_the_gate_only_ever_reads_the_bridge_with_get(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with bridge_serving(triage()) as bridge:
        with start_gate(accounts={"chat": bridge.url}, kinds={"chat": IMESSAGE}) as gate:
            async with agent_client(gate) as client:
                tools = (await client.list_tools()).tools
                await call(client, ACCOUNTS_TOOL)
                await call(client, LIST_TOOL)
                await call(client, COUNT_TOOL, read_status="unread")
                await call(client, GROUP_TOOL, group_by="source_kind")
                bridge.fault = "unavailable"
                await call(client, LIST_TOOL)
                bridge.fault = "forbidden"
                await call(client, LIST_TOOL)
                # An agent has no operation to ask for a write: no tool is one.
                names = [tool.name for tool in tools]

    assert len(bridge.requests) == 5
    assert {(method, path) for method, path, _ in bridge.requests} == {("GET", "/")}
    assert names == [ACCOUNTS_TOOL, LIST_TOOL, COUNT_TOOL, GROUP_TOOL, "list_topics"]


# --- authentication between bridge and gate --------------------------------------------------


def add_credentialed(tmp_path: Path, url: str, secret: str) -> Path:
    credentials = tmp_path / "bridge-token"
    credentials.write_text(secret)
    credentials.chmod(0o600)
    added = operator(
        tmp_path / "glovebox.db",
        *("account", "add", "chat", "--kind", IMESSAGE, "--source", url),
        *("--credentials", str(credentials)),
    )
    assert added.returncode == 0, added.stderr
    return credentials


@pytest.mark.anyio
async def test_the_gate_authenticates_to_the_bridge_and_the_secret_goes_nowhere_else(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    secret = "bridge-secret-0123456789abcdef"
    with bridge_serving(triage(), token=secret) as bridge:
        add_credentialed(tmp_path, bridge.url, secret)
        with start_gate(accounts={"chat": bridge.url}, kinds={"chat": IMESSAGE}) as gate:
            channels = await observe_gate(gate)
            async with agent_client(gate) as client:
                listing = await call(client, LIST_TOOL)

    assert account_of(listing)["status"] == "current"
    assert len(served(listing)) == 4
    assert {auth for _, _, auth in bridge.requests} == {f"Bearer {secret}"}
    everything: dict[str, str | bytes] = {**channels, **storage_files(gate.db)}
    for name, content in everything.items():
        text = content if isinstance(content, str) else content.decode("utf-8", "replace")
        assert secret not in text, name


@pytest.mark.anyio
@pytest.mark.parametrize("presented", [None, "wrong-secret-0123456789abcdef"])
async def test_a_bridge_that_refuses_the_gate_is_blocked_not_empty(
    start_gate: GateStarter, tmp_path: Path, presented: str | None
) -> None:
    with bridge_serving(triage(), token="bridge-secret-0123456789abcdef") as bridge:
        if presented is not None:
            add_credentialed(tmp_path, bridge.url, presented)
        with start_gate(accounts={"chat": bridge.url}, kinds={"chat": IMESSAGE}) as gate:
            async with agent_client(gate) as client:
                result = await call(client, LIST_TOOL)

    assert result["error"] == "source_unavailable"
    assert result["items"] == []
    entry = account_of(result)
    assert (entry["status"], entry["last_sync"]) == ("blocked", None)


def test_a_bridge_token_is_not_sent_in_clear_beyond_this_host(tmp_path: Path) -> None:
    credentials = tmp_path / "bridge-token"
    credentials.write_text("bridge-secret-0123456789abcdef")
    credentials.chmod(0o600)

    refused = operator(
        tmp_path / "glovebox.db",
        *("account", "add", "chat", "--kind", IMESSAGE, "--source", "http://192.0.2.7:9106"),
        *("--credentials", str(credentials)),
    )
    allowed = operator(
        tmp_path / "glovebox.db",
        *("account", "add", "chat", "--kind", IMESSAGE, "--source", "https://bridge.example.test"),
        *("--credentials", str(credentials)),
    )

    assert refused.returncode == 1
    assert "https" in refused.stderr
    assert allowed.returncode == 0, allowed.stderr


# --- freshness, blocked, cancellation and restart ---------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("fault", "status"),
    [("unavailable", "stale"), ("garbage", "stale"), ("forbidden", "blocked")],
)
async def test_an_unreachable_bridge_is_stale_or_blocked_and_never_an_empty_result(
    start_gate: GateStarter, fault: str, status: str
) -> None:
    with bridge_serving(triage()) as bridge:
        with start_gate(accounts={"chat": bridge.url}, kinds={"chat": IMESSAGE}) as gate:
            async with agent_client(gate) as client:
                before = await call(client, LIST_TOOL)
                bridge.fault = fault
                down = {
                    tool: await call(client, tool, **arguments)
                    for tool, arguments in (
                        (LIST_TOOL, {}),
                        (COUNT_TOOL, {}),
                        (GROUP_TOOL, {"group_by": "thread_handle"}),
                    )
                }
                bridge.fault = None
                after = await call(client, LIST_TOOL)

            for tool, result in down.items():
                assert result["error"] == "source_unavailable", tool
                entry = account_of(result)
                assert (entry["status"], entry["last_sync"]) == (status, FIRST_SYNC), tool
            assert down[COUNT_TOOL]["count"] == "0" and down[LIST_TOOL]["items"] == []
            assert after == before
            channels = {name: json.dumps(result) for name, result in down.items()}
            channels["log"] = gate.log.read_text(errors="replace")
            assert_no_corpus(channels)


@pytest.mark.anyio
async def test_a_cancelled_call_leaves_the_gate_usable(start_gate: GateStarter) -> None:
    with bridge_serving(triage()) as bridge:
        with start_gate(accounts={"chat": bridge.url}, kinds={"chat": IMESSAGE}) as gate:
            async with agent_client(gate) as client:
                bridge.delay = 1.0
                with anyio.move_on_after(0.2) as scope:
                    await call(client, LIST_TOOL)
                assert scope.cancelled_caught
                bridge.delay = 0.0
                result = await call(client, LIST_TOOL)

    assert result["error"] == "none" and len(served(result)) == 4
    assert account_of(result)["status"] == "current"
    assert {(method, path) for method, path, _ in bridge.requests} == {("GET", "/")}


@pytest.mark.anyio
async def test_a_gate_restart_keeps_the_last_sync_and_reads_again(
    start_gate: GateStarter,
) -> None:
    with bridge_serving(triage()) as bridge:
        with start_gate(accounts={"chat": bridge.url}, kinds={"chat": IMESSAGE}) as gate:
            async with agent_client(gate) as client:
                first = await call(client, LIST_TOOL)
        bridge.fault = "unavailable"
        with start_gate(
            accounts={"chat": bridge.url},
            kinds={"chat": IMESSAGE},
            now="2026-01-10T12:30:00Z",
        ) as gate:
            async with agent_client(gate) as client:
                down = await call(client, COUNT_TOOL)
                bridge.fault = None
                after = await call(client, LIST_TOOL)

    assert down["error"] == "source_unavailable"
    assert account_of(down)["last_sync"] == FIRST_SYNC
    assert account_of(down)["status"] == "stale"
    assert served(after) == served(first)
    assert account_of(after)["last_sync"] == "2026-01-10T12:30Z"


# --- account scope -----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_token_reaches_only_the_imessage_accounts_it_covers(
    start_gate: GateStarter,
) -> None:
    other = messenger(
        conversation("dm-x", ["+15550109", CAROL], say("x1", CAROL, "2026-01-08T10:00:00Z")),
        account="+15550109",
    )
    with bridge_serving(triage()) as mine, bridge_serving(other) as theirs:
        with start_gate(
            accounts={"mine": mine.url, "theirs": theirs.url},
            kinds={"mine": IMESSAGE, "theirs": IMESSAGE},
            tokens={"both": ["mine", "theirs"], "mine": ["mine"], "theirs": ["theirs"]},
        ) as gate:
            async with agent_client(gate, token=gate.tokens["mine"]) as client:
                only_mine = await call(client, LIST_TOOL)
                (handle,) = [entry["account_handle"] for entry in only_mine["accounts"]]
            async with agent_client(gate, token=gate.tokens["theirs"]) as client:
                (theirs_handle,) = [
                    entry["account_handle"]
                    for entry in (await call(client, ACCOUNTS_TOOL))["accounts"]
                ]
                asked_for_mine = await call(client, LIST_TOOL, account_handle=handle)
            async with agent_client(gate, token=gate.tokens["both"]) as client:
                both = await call(client, COUNT_TOOL)
        theirs_requests = len(theirs.requests)

    assert len(served(only_mine)) == 4
    # Another account's handle selects nothing, exactly as one that does not exist does.
    assert theirs_handle != handle
    assert served(asked_for_mine) == [] and asked_for_mine["accounts"] == []
    assert both["count"] == "5"
    # A token that does not cover an account never causes it to be read.
    assert theirs_requests == 1  # only the call of the token that covers both


# --- the hostile corpus through the bridge -------------------------------------------------------


def hostile_bridge_document() -> dict[str, object]:
    """Corpus strings in every field a chat participant or the bridge writes."""
    conversations: list[dict[str, object]] = []
    for index, text in enumerate(CORPUS.values()):
        conversations.append(
            {
                "id": f"chat-{text}",
                "group": True,
                "name": text,
                "participants": [f"+{text}", f"{text} <p-{text}>"],
                "messages": [
                    {
                        "id": f"guid-{text}",
                        "from": f"+{text}",
                        "sender_name": text,
                        "sent": f"2026-01-08T08:{index:02d}:00Z",
                        "text": text,
                        "caption": text,
                        "reactions": [{"emoji": text, "from": text, "type": text}],
                        "read": text,
                        "service": text,
                        "media": [{"filename": text, "mime": text, "type": f"{text}/{text}"}],
                        "error": text,
                    }
                ],
            }
        )
    return {"account": f"{CORPUS['marker']}@example.test", "conversations": conversations}


@pytest.mark.anyio
async def test_no_corpus_string_reaches_any_channel_from_a_hostile_bridge(
    start_gate: GateStarter,
) -> None:
    with bridge_serving(hostile_bridge_document()) as bridge:
        with start_gate(accounts={"chat": bridge.url}, kinds={"chat": IMESSAGE}) as gate:
            channels = await observe_gate(gate)
            async with agent_client(gate) as client:
                listing = await call(client, LIST_TOOL)
            # The bridge's own error text, in a status line, headers and body.
            bridge.fault = "unavailable"
            async with agent_client(gate) as client:
                outage = await call(client, LIST_TOOL)
            bridge.fault = "forbidden"
            async with agent_client(gate) as client:
                refused = await call(client, LIST_TOOL)
    files = storage_files(gate.db)

    assert len(served(listing)) == len(CORPUS)
    assert {item["has_attachments"] for item in served(listing)} == {True}
    assert {item["read_status"] for item in served(listing)} == {"unknown"}
    assert outage["error"] == refused["error"] == "source_unavailable"
    channels["outage"] = json.dumps(outage)
    channels["refused"] = json.dumps(refused)
    channels["log"] = gate.log.read_text(errors="replace")
    assert_no_corpus(channels)
    assert_no_corpus(files)


# --- the local Messages database ---------------------------------------------------------------


SCHEMA = """
create table handle (ROWID integer primary key autoincrement, id text, service text);
create table chat (
    ROWID integer primary key autoincrement, guid text, style integer,
    chat_identifier text, display_name text
);
create table message (
    ROWID integer primary key autoincrement, guid text, text text, attributedBody blob,
    handle_id integer default 0, date integer, is_read integer default 0,
    is_from_me integer default 0, cache_has_attachments integer default 0,
    item_type integer default 0, associated_message_type integer default 0, service text
);
create table chat_message_join (chat_id integer, message_id integer);
create table attachment (
    ROWID integer primary key autoincrement, filename text, mime_type text, transfer_name text
);
create table message_attachment_join (message_id integer, attachment_id integer);
"""


def apple_time(when: str) -> int:
    """Nanoseconds since 2001-01-01 UTC, as current macOS stores a message's date."""
    moment = datetime.fromisoformat(when).astimezone(UTC)
    return int((moment - APPLE_EPOCH).total_seconds()) * 1_000_000_000


def typed_text(text: str) -> bytes:
    """The `attributedBody` blob Messages keeps when it leaves the `text` column empty."""
    body = text.encode()
    length = bytes([len(body)]) if len(body) < 0x80 else b"\x81" + len(body).to_bytes(2, "little")
    return (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84"
        b"\x08NSObject\x00\x85\x92\x84\x84\x84\x08NSString\x01\x94\x84\x01+"
        + length
        + body
        + b"\x86\x84\x02iI\x01"
        + bytes([len(body) & 0xFF])
        + b"\x92\x84\x84\x84\x0cNSDictionary\x00\x94\x84\x01i\x01\x92\x84\x96\x96\x1d"
    )


class MessagesDb:
    """A Messages database file with fictional content, written the way Messages writes it."""

    def __init__(self, path: Path) -> None:
        self.path = path
        with self._open() as database:
            database.executescript(SCHEMA)
        self._chats: dict[str, int] = {}
        self._handles: dict[str, int] = {}

    def _open(self) -> sqlite3.Connection:
        database = sqlite3.connect(self.path, isolation_level=None)
        database.execute("pragma journal_mode=wal")
        return database

    def receive(
        self,
        guid: str,
        sender: str,
        when: str,
        *,
        chat: str = "iMessage;-;bob",
        group: bool = False,
        text: str | None = "Hello",
        body: bytes | None = None,
        read: bool = False,
        attachment: str | None = None,
        sent_by_me: bool = False,
        reaction: int = 0,
        item_type: int = 0,
        service: str = "iMessage",
        display_name: str | None = None,
    ) -> None:
        with self._open() as database:
            handle = self._handles.get(sender)
            if handle is None:
                cursor = database.execute(
                    "insert into handle (id, service) values (?, ?)", (sender, service)
                )
                handle = self._handles[sender] = int(cursor.lastrowid or 0)
            chat_id = self._chats.get(chat)
            if chat_id is None:
                cursor = database.execute(
                    "insert into chat (guid, style, chat_identifier, display_name)"
                    " values (?, ?, ?, ?)",
                    (chat, 43 if group else 45, chat, display_name),
                )
                chat_id = self._chats[chat] = int(cursor.lastrowid or 0)
            cursor = database.execute(
                "insert into message (guid, text, attributedBody, handle_id, date, is_read,"
                " is_from_me, cache_has_attachments, item_type, associated_message_type,"
                " service) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    guid,
                    text,
                    body,
                    handle,
                    apple_time(when),
                    int(read),
                    int(sent_by_me),
                    int(attachment is not None),
                    item_type,
                    reaction,
                    service,
                ),
            )
            message_id = cursor.lastrowid
            database.execute("insert into chat_message_join values (?, ?)", (chat_id, message_id))
            if attachment is not None:
                cursor = database.execute(
                    "insert into attachment (filename, mime_type, transfer_name) values (?, ?, ?)",
                    (attachment, attachment, attachment),
                )
                database.execute(
                    "insert into message_attachment_join values (?, ?)",
                    (message_id, cursor.lastrowid),
                )

    def rows(self) -> list[tuple[Any, ...]]:
        """Every message row, to show that reading changed none of them."""
        with self._open() as database:
            return database.execute("select * from message order by ROWID").fetchall()


@pytest.fixture
def messages_db(tmp_path: Path) -> MessagesDb:
    return MessagesDb(tmp_path / "chat.db")


def populate(db: MessagesDb) -> None:
    db.receive("g1", BOB, "2026-01-05T18:00:00Z", chat="iMessage;-;bob", read=True, text="One")
    db.receive(
        "g2", BOB, "2026-01-05T18:05:30Z", chat="iMessage;-;bob", text=None, body=typed_text("Two")
    )
    db.receive("g3", CAROL, "2026-01-06T07:10:00Z", chat="iMessage;-;carol", read=False)
    db.receive(
        "g4",
        BOB,
        "2026-01-07T20:00:00Z",
        chat="chat1234",
        group=True,
        read=True,
        attachment="x.png",
    )
    db.receive("g5", BOB, "2025-11-01T18:00:00Z", chat="iMessage;-;bob")  # before the window


@pytest.mark.anyio
async def test_the_local_messages_database_is_served_as_messenger_items(
    start_gate: GateStarter, messages_db: MessagesDb
) -> None:
    populate(messages_db)
    with start_gate(accounts={"chat": messages_db.path}, kinds={"chat": IMESSAGE}) as gate:
        async with agent_client(gate) as client:
            listing = await call(client, LIST_TOOL, sort="received_asc")
            threads = await call(client, GROUP_TOOL, group_by="thread_handle")
            senders = await call(client, GROUP_TOOL, group_by="sender_handle")

    items = served(listing)
    assert [item["received"] for item in items] == [
        "2026-01-05T18:00Z",
        "2026-01-05T18:05Z",
        "2026-01-06T07:10Z",
        "2026-01-07T20:00Z",
    ]
    assert [item["read_status"] for item in items] == ["read", "unread", "unread", "read"]
    assert [item["has_attachments"] for item in items] == [False, False, False, True]
    assert {item["source_kind"] for item in items} == {IMESSAGE}
    assert sorted(group["count"] for group in threads["groups"]) == ["1", "1", "2"]
    assert sorted(group["count"] for group in senders["groups"]) == ["1", "3"]
    entry = account_of(listing)
    assert (entry["status"], entry["read_only_enforcement"]) == ("current", "gateway")


@pytest.mark.anyio
async def test_a_newly_received_message_appears_and_nothing_is_changed_in_the_database(
    start_gate: GateStarter, messages_db: MessagesDb
) -> None:
    populate(messages_db)
    with start_gate(accounts={"chat": messages_db.path}, kinds={"chat": IMESSAGE}) as gate:
        async with agent_client(gate) as client:
            before = await call(client, COUNT_TOOL)
            unread_before = await call(client, COUNT_TOOL, read_status="unread")
            messages_db.receive("new", BOB, "2026-01-10T11:58:00Z", text="Just arrived")
            after = await call(client, COUNT_TOOL)
            unread_after = await call(client, COUNT_TOOL, read_status="unread")
            newest = await call(client, LIST_TOOL, limit=1)
            rows = messages_db.rows()
            await call(client, LIST_TOOL)

    assert (before["count"], after["count"]) == ("4", "5")
    assert (unread_before["count"], unread_after["count"]) == ("2", "3")
    assert served(newest)[0]["received"] == "2026-01-10T11:58Z"
    # Below the seam: the gate did not mark anything read or alter any row.
    assert messages_db.rows() == rows
    assert [row[6] for row in rows] == [1, 0, 0, 1, 0, 0]  # is_read, as it was written


@pytest.mark.anyio
async def test_only_received_messages_are_items(
    start_gate: GateStarter, messages_db: MessagesDb
) -> None:
    messages_db.receive("in", BOB, "2026-01-05T18:00:00Z")
    messages_db.receive("mine", OPERATOR_ID, "2026-01-05T18:01:00Z", sent_by_me=True)
    messages_db.receive("tapback", BOB, "2026-01-05T18:02:00Z", reaction=2000, text="Loved it")
    messages_db.receive("untapback", BOB, "2026-01-05T18:03:00Z", reaction=3001, text="Removed")
    messages_db.receive("renamed", BOB, "2026-01-05T18:04:00Z", item_type=2, text=None)
    with start_gate(accounts={"chat": messages_db.path}, kinds={"chat": IMESSAGE}) as gate:
        async with agent_client(gate) as client:
            result = await call(client, LIST_TOOL)

    assert [item["received"] for item in served(result)] == ["2026-01-05T18:00Z"]
    # What is not a message is left out silently; it is not an item that could not be read.
    assert account_of(result)["status"] == "current"
    assert account_of(result)["skipped_items"] == "0"


@pytest.mark.anyio
async def test_a_message_without_a_sender_or_conversation_is_skipped_and_counted(
    start_gate: GateStarter, messages_db: MessagesDb
) -> None:
    messages_db.receive("ok", BOB, "2026-01-05T18:00:00Z")
    with sqlite3.connect(messages_db.path, isolation_level=None) as database:
        database.execute(
            "insert into message (guid, text, handle_id, date) values ('no-chat', 'x', 1, ?)",
            (apple_time("2026-01-05T18:01:00Z"),),
        )
        database.execute(
            "insert into message (guid, text, handle_id, date) values ('no-sender', 'x', 0, ?)",
            (apple_time("2026-01-05T18:02:00Z"),),
        )
        database.execute("insert into chat_message_join values (1, 3)")
    with start_gate(accounts={"chat": messages_db.path}, kinds={"chat": IMESSAGE}) as gate:
        async with agent_client(gate) as client:
            result = await call(client, LIST_TOOL)

    assert len(served(result)) == 1
    entry = account_of(result)
    assert (entry["status"], entry["skipped_items"]) == ("incomplete", "2")


@pytest.mark.anyio
async def test_a_database_the_gate_may_not_read_or_that_is_missing_is_blocked(
    start_gate: GateStarter, messages_db: MessagesDb, tmp_path: Path
) -> None:
    """Below the seam on purpose: the file mode stands for the permission macOS withholds."""
    populate(messages_db)
    with start_gate(accounts={"chat": messages_db.path}, kinds={"chat": IMESSAGE}) as gate:
        async with agent_client(gate) as client:
            current = account_of(await call(client, COUNT_TOOL))
            messages_db.path.chmod(0o000)
            denied = await call(client, COUNT_TOOL)
            messages_db.path.chmod(0o600)
            moved = messages_db.path.rename(tmp_path / "moved.db")
            missing = await call(client, LIST_TOOL)
            moved.rename(messages_db.path)
            after = await call(client, COUNT_TOOL)

    for down in (denied, missing):
        assert down["error"] == "source_unavailable"
        assert account_of(down)["status"] == "blocked"
        assert account_of(down)["last_sync"] == current["last_sync"]
    assert account_of(after) == current


@pytest.mark.anyio
async def test_a_database_that_cannot_be_understood_is_stale_with_fixed_text(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    broken = tmp_path / "chat.db"
    broken.write_bytes(CORPUS["injection"].encode() * 100)
    with start_gate(accounts={"chat": broken}, kinds={"chat": IMESSAGE}) as gate:
        async with agent_client(gate) as client:
            result = await call(client, LIST_TOOL)

    assert result["error"] == "source_unavailable"
    assert account_of(result)["status"] == "stale"
    assert_no_corpus({"result": json.dumps(result), "log": gate.log.read_text(errors="replace")})


@pytest.mark.anyio
async def test_a_hostile_messages_database_leaks_no_corpus_string_to_any_channel(
    start_gate: GateStarter, messages_db: MessagesDb
) -> None:
    for index, text in enumerate(CORPUS.values()):
        when = f"2026-01-08T08:{index:02d}:00Z"
        messages_db.receive(
            f"guid-{text}",
            f"+{text}",
            when,
            chat=f"chat-{text}",
            group=True,
            text=text,
            display_name=text,
            attachment=text,
            service=text,
        )
        messages_db.receive(
            f"body-{text}",
            f"+{text}",
            when,
            chat=f"chat-{text}",
            group=True,
            text=None,
            body=typed_text(text[:100]),
        )
        messages_db.receive(
            f"tapback-{text}", f"+{text}", when, chat=f"chat-{text}", reaction=2001, text=text
        )
    with start_gate(accounts={"chat": messages_db.path}, kinds={"chat": IMESSAGE}) as gate:
        channels = await observe_gate(gate)
        async with agent_client(gate) as client:
            listing = await call(client, LIST_TOOL)
    files = storage_files(gate.db)

    assert listing["error"] == "none"
    assert len(served(listing)) == 2 * len(CORPUS)
    assert {item["source_kind"] for item in served(listing)} == {IMESSAGE}
    assert_no_corpus(channels)
    assert_no_corpus(files)


@pytest.mark.anyio
async def test_no_message_text_is_stored_by_the_gate(
    start_gate: GateStarter, messages_db: MessagesDb
) -> None:
    messages_db.receive("g1", BOB, "2026-01-05T18:00:00Z", text="Quokka-7731-verbatim")
    messages_db.receive(
        "g2", BOB, "2026-01-05T18:01:00Z", text=None, body=typed_text("Wombat-4419-verbatim")
    )
    with start_gate(accounts={"chat": messages_db.path}, kinds={"chat": IMESSAGE}) as gate:
        async with agent_client(gate) as client:
            await call(client, LIST_TOOL)
    stored = b"".join(storage_files(gate.db).values())
    log = gate.log.read_bytes()

    assert b"Quokka-7731" not in stored + log
    assert b"Wombat-4419" not in stored + log
    assert BOB.encode() not in stored + log


# --- how the database is opened ------------------------------------------------------


def test_the_database_connection_cannot_write(messages_db: MessagesDb) -> None:
    populate(messages_db)
    before = messages_db.path.read_bytes()
    statements = [
        "update message set is_read = 1",
        "insert into handle (id) values ('x')",
        "delete from message",
        "create table scratch (a)",
        "drop table message",
        "attach database ':memory:' as other",
        "pragma journal_mode = delete",
        "pragma writable_schema = on",
    ]
    with chatdb.open_read_only(messages_db.path) as database:
        assert database.execute("select count(*) from message").fetchone() == (5,)
        for statement in statements:
            with pytest.raises(sqlite3.DatabaseError):
                database.execute(statement)

    assert messages_db.path.read_bytes() == before
    assert messages_db.rows()[0][6] == 1


def test_a_date_in_seconds_is_still_read(messages_db: MessagesDb) -> None:
    messages_db.receive("g1", BOB, "2026-01-05T18:00:00Z")
    with sqlite3.connect(messages_db.path, isolation_level=None) as database:
        seconds = int((datetime(2026, 1, 6, 9, 30, tzinfo=UTC) - APPLE_EPOCH).total_seconds())
        database.execute("update message set date = ? where ROWID = 1", (seconds,))

    mailbox = chatdb.read_messages(messages_db.path, datetime(2025, 12, 1, tzinfo=UTC))

    assert [message.received for message in mailbox.messages] == [
        datetime(2026, 1, 6, 9, 30, tzinfo=UTC)
    ]


@pytest.mark.parametrize("length", [0, 1, 127, 128, 300, 5000])
def test_text_is_read_from_the_typed_body(length: int) -> None:
    text = "é" * (length // 2) + "x" * (length % 2)

    assert chatdb.text_of_body(typed_text(text)) == text


@pytest.mark.parametrize(
    "blob",
    [
        b"",
        b"NSString",
        b"NSString\x01\x94\x84\x01+",
        b"NSString\x01\x94\x84\x01+\x81\xff\xff",
        b"\xff" * 64,
    ],
)
def test_a_malformed_typed_body_is_no_text_and_no_error(blob: bytes) -> None:
    assert chatdb.text_of_body(blob) == ""


def window_start(days: int) -> datetime:
    return datetime(2026, 1, 10, 12, tzinfo=UTC) - timedelta(days=days)


def test_more_messages_than_the_cap_are_reported_as_missing(
    messages_db: MessagesDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    for number in range(5):
        messages_db.receive(f"g{number}", BOB, f"2026-01-05T18:0{number}:00Z")
    monkeypatch.setattr(chatdb, "MAX_MESSAGES", 3)

    mailbox = chatdb.read_messages(messages_db.path, window_start(30))

    assert [message.guid for message in mailbox.messages] == ["g4", "g3", "g2"]
    assert mailbox.skipped == 2


def test_names_filenames_and_types_are_not_selected_at_all() -> None:
    selected = chatdb.QUERY.lower()

    for column in ("display_name", "chat_identifier", "filename", "mime_type", "transfer_name"):
        assert column not in selected
    assert "is_from_me, 0) = 0" in selected


# --- the operator's acceptance script ------------------------------------------------------


CHECK = Path(__file__).resolve().parent.parent / "scripts" / "imessage_check.py"


def run_check(gate: RunningGate) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECK), "--url", gate.url],
        env={**os.environ, "GLOVEBOX_CHECK_TOKEN": gate.token},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.anyio
async def test_the_acceptance_script_prints_permitted_fields_and_the_status(
    start_gate: GateStarter, messages_db: MessagesDb
) -> None:
    messages_db.receive("g1", BOB, "2026-01-05T18:00:00Z", text="Quokka-7731-verbatim")
    with start_gate(accounts={"chat": messages_db.path}, kinds={"chat": IMESSAGE}) as gate:
        current = await anyio.to_thread.run_sync(run_check, gate)
        messages_db.path.chmod(0o000)
        blocked = await anyio.to_thread.run_sync(run_check, gate)
        messages_db.path.chmod(0o600)

    assert current.returncode == 0, current.stderr
    shown = json.loads(current.stdout)
    assert shown["accounts"][0]["status"] == "current"
    assert [item["received"] for item in shown["items"]] == ["2026-01-05T18:00Z"]
    assert "Quokka" not in current.stdout and BOB not in current.stdout
    assert blocked.returncode == 1
    assert json.loads(blocked.stdout)["accounts"][0]["status"] == "blocked"
    assert json.loads(blocked.stdout)["items"] == []
