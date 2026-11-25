"""Acceptance tests of fixture messenger items served through the shared read tools (ADR 0006).

Everything goes through MCP tool calls from a real client. A messenger account is one of
source kind `fixture_messenger`: a document of conversations, each with messages. Its items
are the same fields as mail items, listed, counted, grouped and paged by the same tools. Tests
that read the database say so: what is stored is not visible through a tool call.
"""

import json
import re
import sqlite3
from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from tests.client import ALL_TOOLS, COUNT_TOOL, GROUP_TOOL, LIST_TOOL, agent_client
from tests.conftest import GateStarter, RunningGate
from tests.hostile import (
    CORPUS,
    assert_no_corpus,
    hostile_mail_fixture,
    hostile_messenger_fixture,
    observe_gate,
)
from tests.schema import closed_schema_problems
from tests.test_classifier import GOOD, GOOD_SERVED, Setup, digest
from tests.test_gate import ITEM_FIELDS, storage_files, write_fixture
from tests.test_queries import call, mail, mailbox
from tests.test_standins import CAST, run_seed, serve

HANDLE = re.compile(r"^[a-z2-7]{24}$")
MESSENGER = "fixture_messenger"
OPERATOR = "+15550100"


def say(
    label: str,
    sender: str,
    sent: str,
    *,
    read: bool | None = None,
    media: Sequence[dict[str, object]] | None = None,
    text: str | None = None,
    caption: str | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "id": label,
        "from": sender,
        "sent": sent,
        "text": text if text is not None else f"Text {label}",
    }
    if read is not None:
        message["read"] = read
    if media is not None:
        message["media"] = list(media)
    if caption is not None:
        message["caption"] = caption
    return message


def conversation(
    label: str, participants: Sequence[str], *messages: dict[str, object], group: bool = False
) -> dict[str, object]:
    return {
        "id": label,
        "group": group,
        "name": f"Name of {label}",
        "participants": list(participants),
        "messages": list(messages),
    }


def messenger(*conversations: dict[str, object], account: str = OPERATOR) -> dict[str, object]:
    return {"account": account, "conversations": list(conversations)}


def triage_messenger() -> dict[str, object]:
    """Two direct conversations and one group, five messages in the window and one older."""
    return messenger(
        conversation(
            "dm-dave",
            [OPERATOR, "+15550103"],
            say("s1", "+15550103", "2026-01-05T18:00:00Z", read=True),
            say("s2", "+15550103", "2026-01-05T18:05:30Z", read=False),
            say("old", "+15550103", "2025-11-01T18:00:00Z", read=False),
        ),
        conversation(
            "dm-carol",
            [OPERATOR, "+15550102"],
            say("s3", "+15550102", "2026-01-06T07:10:00Z"),
        ),
        conversation(
            "club",
            [OPERATOR, "+15550103", "+15550102"],
            say("s4", "+15550103", "2026-01-07T20:00:00Z", media=[{"type": "image/png"}]),
            say("s5", "+15550102", "2026-01-07T20:01:00Z"),
            group=True,
        ),
    )


def publish(
    start_gate: GateStarter,
    tmp_path: Path,
    document: dict[str, object],
    **options: Any,
) -> AbstractContextManager[RunningGate]:
    """Start a gate whose one account is a messenger serving `document`."""
    source = write_fixture(tmp_path / "messenger.json", document)
    return start_gate(
        accounts={"chat": source},
        kinds={"chat": MESSENGER},
        tokens={"default": ["chat"]},
        **options,
    )


def publish_both(
    start_gate: GateStarter,
    tmp_path: Path,
    mail_document: dict[str, object],
    messenger_document: dict[str, object],
    **options: Any,
) -> AbstractContextManager[RunningGate]:
    """Start a gate with a mail account and a messenger account under one token."""
    return start_gate(
        accounts={
            "mail": write_fixture(tmp_path / "mail.json", mail_document),
            "chat": write_fixture(tmp_path / "messenger.json", messenger_document),
        },
        kinds={"chat": MESSENGER},
        tokens={"default": ["mail", "chat"]},
        **options,
    )


async def listed(client: Client, **arguments: object) -> list[dict[str, Any]]:
    result = await call(client, LIST_TOOL, sort="received_asc", **arguments)
    assert result["error"] == "none"
    items: list[dict[str, Any]] = result["items"]
    return items


def texts(items: Sequence[dict[str, Any]]) -> list[str]:
    return [item["received"] for item in items]


# --- the shared item shape --------------------------------------------------------------


@pytest.mark.anyio
async def test_messenger_items_have_the_fields_of_mail_items_with_a_conversation_handle(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, triage_messenger()) as gate:
        async with agent_client(gate) as client:
            items = await listed(client)
            accounts = await call(client, "list_accounts")

    assert texts(items) == [
        "2026-01-05T18:00Z",
        "2026-01-05T18:05Z",
        "2026-01-06T07:10Z",
        "2026-01-07T20:00Z",
        "2026-01-07T20:01Z",
    ]
    for item in items:
        assert set(item) == ITEM_FIELDS
        assert item["source_kind"] == MESSENGER
        for name in ("item_handle", "account_handle", "sender_handle", "thread_handle"):
            assert HANDLE.match(item[name])
        # Nothing here is classified: no local runtime answers in the test harness.
        assert item["classification_status"] == "unavailable"
    assert len({item["item_handle"] for item in items}) == 5
    # The conversation handle takes the place of the thread handle: one per conversation,
    # shared by every message of it, for direct and group conversations alike.
    dave, carol, club = items[0:2], items[2:3], items[3:5]
    assert len({item["thread_handle"] for item in dave}) == 1
    assert len({item["thread_handle"] for item in club}) == 1
    assert len({item["thread_handle"] for item in items}) == 3
    assert dave[0]["sender_handle"] == dave[1]["sender_handle"]
    assert len({item["sender_handle"] for item in club}) == 2
    assert carol[0]["sender_handle"] not in {item["sender_handle"] for item in dave}
    # A sender is one handle across the conversations of the account.
    assert club[0]["sender_handle"] == dave[0]["sender_handle"]
    assert club[1]["sender_handle"] == carol[0]["sender_handle"]
    assert [entry["source_kind"] for entry in accounts["accounts"]] == [MESSENGER]
    assert accounts["accounts"][0]["status"] == "current"


@pytest.mark.anyio
async def test_read_status_is_unknown_where_the_messenger_has_none(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, triage_messenger()) as gate:
        async with agent_client(gate) as client:
            items = await listed(client)
            grouped = await call(client, GROUP_TOOL, group_by="read_status")
            unread = await listed(client, read_status="unread")
            unknown = await listed(client, read_status="unknown")

    assert [item["read_status"] for item in items] == [
        "read",
        "unread",
        "unknown",
        "unknown",
        "unknown",
    ]
    assert grouped["groups"] == [
        {"key": "unknown", "count": "3"},
        {"key": "read", "count": "1"},
        {"key": "unread", "count": "1"},
    ]
    assert texts(unread) == ["2026-01-05T18:05Z"]
    assert len(unknown) == 3


@pytest.mark.anyio
async def test_media_messages_report_attachment_presence_and_nothing_of_the_media(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    document = messenger(
        conversation(
            "dm",
            [OPERATOR, "+15550103"],
            say("plain", "+15550103", "2026-01-05T10:00:00Z"),
            say("empty", "+15550103", "2026-01-05T10:01:00Z", media=[]),
            say("png", "+15550103", "2026-01-05T10:02:00Z", media=[{"type": "image/png"}]),
            say("odd", "+15550103", "2026-01-05T10:03:00Z", media=[{"type": "x/" + "q" * 300}]),
            say("none", "+15550103", "2026-01-05T10:04:00Z", media=[{}], caption="A caption"),
            say("many", "+15550103", "2026-01-05T10:05:00Z", media=[{"type": "audio/ogg"}] * 3),
        )
    )
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            items = await listed(client)
            with_media = await listed(client, has_attachments=True)
            tools = (await client.list_tools()).tools
            counted = await call(client, COUNT_TOOL, has_attachments=False)

    assert [item["has_attachments"] for item in items] == [False, False, True, True, True, True]
    assert len(with_media) == 4
    assert counted["count"] == "2"
    # No media type or other media value exists in a result or a schema.
    served = json.dumps(items) + json.dumps([tool.model_dump() for tool in tools])
    for value in ("image/png", "audio/ogg", "x/qqq", "caption"):
        assert value not in served
    for tool in tools:
        assert tool.output_schema is not None
        assert closed_schema_problems(tool.output_schema) == []
        assert "media" not in json.dumps(tool.output_schema)


@pytest.mark.anyio
async def test_a_messenger_message_without_an_id_keeps_its_handle_across_calls(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    anonymous = messenger(
        conversation(
            "dm",
            [OPERATOR, "+15550103"],
            {"from": "+15550103", "sent": "2026-01-05T10:00:00Z", "text": "same"},
            {"from": "+15550103", "sent": "2026-01-05T10:00:00Z", "text": "same"},
            {"from": "+15550103", "sent": "2026-01-05T10:00:30Z", "text": "other"},
        )
    )
    with publish(start_gate, tmp_path, anonymous) as gate:
        async with agent_client(gate) as client:
            first = await listed(client)
            second = await listed(client)

    # Two messages alike in sender and minute are still two items, and each keeps its handle.
    assert len(first) == 3
    assert len({item["item_handle"] for item in first}) == 3
    assert first == second


# --- one call across mail and messenger -------------------------------------------------


def two_sources() -> tuple[dict[str, object], dict[str, object]]:
    mails = mailbox(
        [
            mail("m1", "bob", "2026-01-05T09:00:00Z", read=True, thread="t1"),
            mail("m2", "bob", "2026-01-06T10:30:00Z", read=False, attachments=True, thread="t1"),
            mail("m3", "carol", "2026-01-07T20:00:30Z", thread="t2"),
        ]
    )
    return mails, triage_messenger()


@pytest.mark.anyio
async def test_one_call_lists_filters_groups_counts_and_pages_across_both_kinds(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    mails, chats = two_sources()
    with publish_both(start_gate, tmp_path, mails, chats) as gate:
        async with agent_client(gate) as client:
            everything = await listed(client)
            accounts = {
                entry["source_kind"]: entry["account_handle"]
                for entry in (await call(client, "list_accounts"))["accounts"]
            }
            by_kind = await call(client, GROUP_TOOL, group_by="source_kind")
            by_thread = await call(client, GROUP_TOOL, group_by="thread_handle")
            by_sender = await call(client, GROUP_TOOL, group_by="sender_handle")
            counted = await call(client, COUNT_TOOL)
            unread = await listed(client, read_status="unread")
            attached = await listed(client, has_attachments=True)
            only_chat = await listed(client, account_handle=accounts[MESSENGER])
            only_mail = await listed(client, account_handle=accounts["fixture_mail"])
            late = await listed(client, received_from="2026-01-07T00:00Z")
            # Paging walks both kinds in one order, without repeating or dropping an item.
            seen: list[str] = []
            page = await call(client, LIST_TOOL, sort="received_asc", limit=3)
            while True:
                seen += [item["item_handle"] for item in page["items"]]
                if page["next_cursor"] is None:
                    break
                page = await call(
                    client, LIST_TOOL, sort="received_asc", limit=3, cursor=page["next_cursor"]
                )

    assert len(everything) == 8
    assert {item["source_kind"] for item in everything} == {"fixture_mail", MESSENGER}
    assert texts(everything) == sorted(texts(everything))
    assert counted["count"] == "8"
    # Mail threads and messenger conversations are grouped together, as are their senders.
    assert [group["count"] for group in by_thread["groups"]] == ["2", "2", "2", "1", "1"]
    assert [group["count"] for group in by_sender["groups"]] == ["3", "2", "2", "1"]
    assert by_kind["groups"] == [
        {"key": MESSENGER, "count": "5"},
        {"key": "fixture_mail", "count": "3"},
    ]
    assert sorted(item["source_kind"] for item in unread) == ["fixture_mail", MESSENGER]
    assert sorted(item["source_kind"] for item in attached) == ["fixture_mail", MESSENGER]
    assert len(only_chat) == 5 and len(only_mail) == 3
    assert {item["source_kind"] for item in late} == {"fixture_mail", MESSENGER}
    assert seen == [item["item_handle"] for item in everything]


@pytest.mark.anyio
async def test_a_token_that_covers_only_one_kind_sees_only_that_kind(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    mails, chats = two_sources()
    accounts = {
        "mail": write_fixture(tmp_path / "mail.json", mails),
        "chat": write_fixture(tmp_path / "messenger.json", chats),
    }
    tokens = {"both": ["mail", "chat"], "chat-only": ["chat"]}
    with start_gate(accounts=accounts, kinds={"chat": MESSENGER}, tokens=tokens) as gate:
        async with agent_client(gate, gate.tokens["chat-only"]) as client:
            only = await listed(client)
            listing = await call(client, "list_accounts")
        async with agent_client(gate, gate.tokens["both"]) as client:
            both = await listed(client)

    assert {item["source_kind"] for item in only} == {MESSENGER}
    assert [entry["source_kind"] for entry in listing["accounts"]] == [MESSENGER]
    assert len(both) == 8


# --- sender handles across accounts -----------------------------------------------------


@pytest.mark.anyio
async def test_the_same_person_in_two_accounts_has_unrelated_handles_and_stable_ones_within(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    first = messenger(
        conversation("dm", [OPERATOR, "+15550103"], say("a1", "+15550103", "2026-01-05T10:00:00Z")),
        account=OPERATOR,
    )
    second = messenger(
        conversation("dm", [OPERATOR, "+15550103"], say("a1", "+15550103", "2026-01-05T10:00:00Z")),
        account="+15550101",
    )
    third = mailbox([mail("a1", "dave", "2026-01-05T10:00:00Z", thread="dm")])
    accounts = {
        "one": write_fixture(tmp_path / "one.json", first),
        "two": write_fixture(tmp_path / "two.json", second),
        "mail": write_fixture(tmp_path / "mail.json", third),
    }
    kinds = {"one": MESSENGER, "two": MESSENGER}
    with start_gate("a.db", accounts=accounts, kinds=kinds) as gate:
        async with agent_client(gate) as client:
            before = await listed(client)
    with start_gate("a.db", accounts=accounts, kinds=kinds) as gate:
        async with agent_client(gate) as client:
            after = await listed(client)

    assert before == after
    assert len(before) == 3
    for key in ("sender_handle", "thread_handle", "item_handle"):
        assert len({item[key] for item in before}) == 3, key


# --- the rolling window -----------------------------------------------------------------


def window_messenger() -> dict[str, object]:
    """Messages around the 30-day boundary of the pinned clock, 2026-01-10T12:00Z."""
    return messenger(
        conversation(
            "dm",
            [OPERATOR, "+15550103"],
            say("before", "+15550103", "2025-12-11T11:59:59Z"),
            say("first", "+15550103", "2025-12-11T12:00:00Z"),
            say("inside", "+15550103", "2026-01-09T08:00:00Z"),
            say("now", "+15550103", "2026-01-10T12:00:30Z"),
            say("ahead", "+15550103", "2026-01-10T12:01:00Z"),
            say("ancient", "+15550103", "2025-01-01T00:00:00Z"),
        )
    )


@pytest.mark.anyio
async def test_messages_outside_the_rolling_thirty_day_window_are_neither_served_nor_ingested(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, window_messenger()) as gate:
        async with agent_client(gate) as client:
            items = await listed(client)
            counted = await call(client, COUNT_TOOL, received_from="2000-01-01T00:00Z")
    # Below the seam on purpose: not being ingested is a property of the stored rows.
    with sqlite3.connect(gate.db) as database:
        stored = [row[0] for row in database.execute("select received from items order by 1")]
        minted = database.execute("select count(*) from handles where kind = 'item'").fetchone()[0]

    assert texts(items) == ["2025-12-11T12:00Z", "2026-01-09T08:00Z", "2026-01-10T12:00Z"]
    assert counted["count"] == "3"
    assert stored == texts(items)
    assert minted == 3
    # A minute later the oldest message has left the window.
    with publish(start_gate, tmp_path, window_messenger(), now="2026-01-10T12:01:00Z") as gate:
        async with agent_client(gate) as client:
            later = await listed(client)
    assert texts(later) == ["2026-01-09T08:00Z", "2026-01-10T12:00Z", "2026-01-10T12:01Z"]


# --- semantic fields --------------------------------------------------------------------


@pytest.mark.anyio
async def test_messenger_items_carry_validated_semantic_fields_from_text_and_caption(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    setup = Setup(tmp_path)
    setup.rule("marker-text", GOOD)
    setup.rule("marker-caption", GOOD)
    setup.rule("marker-bad", "free-form prose, not a classification")
    document = messenger(
        conversation(
            "dm",
            [OPERATOR, "+15550103"],
            say("a", "+15550103", "2026-01-05T10:00:00Z", text="marker-text"),
            say(
                "b",
                "+15550103",
                "2026-01-05T10:01:00Z",
                text="",
                caption="marker-caption",
                media=[{}],
            ),
            say("c", "+15550103", "2026-01-05T10:02:00Z", text="marker-bad"),
            say(
                "d",
                "+15550103",
                "2026-01-05T10:03:00Z",
                text="nothing of note",
                caption="and a caption",
            ),
        )
    )
    with publish(start_gate, tmp_path, document, extra_env=setup.env()) as gate:
        async with agent_client(gate) as client:
            items = await listed(client)
            billing = await listed(client, topic="billing")
            grouped = await call(client, GROUP_TOOL, group_by="classification_status")

    served = [
        (item["topic"], item["reply_needed"], item["deadline"], item["classification_status"])
        for item in items
    ]
    good = (GOOD["topic"], GOOD["reply_needed"], GOOD_SERVED["deadline"], "classified")
    assert served[:2] == [good, good]
    assert served[2][3] == "invalid" and served[2][:3] == ("unknown", "unknown", "unknown")
    assert len(billing) == 2
    assert sum(int(group["count"]) for group in grouped["groups"]) == 4
    # Below the seam, in the stub's own record: the classifier was shown each message's text
    # and caption, and nothing else of the messenger (no sender, no conversation name).
    assert {recorded["text_sha256"] for recorded in setup.calls()} == {
        digest(text)
        for text in (
            "marker-text",
            "marker-caption",
            "marker-bad",
            "nothing of note\n\nand a caption",
        )
    }


# --- failure ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_an_unreadable_messenger_is_an_outage_beside_a_working_mail_account(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    mails, _ = two_sources()
    accounts: dict[str, Path | str] = {
        "mail": write_fixture(tmp_path / "mail.json", mails),
        "chat": tmp_path / "missing.json",
    }
    with start_gate(
        accounts=accounts, kinds={"chat": MESSENGER}, tokens={"default": ["mail", "chat"]}
    ) as gate:
        async with agent_client(gate) as client:
            result = await client.call_tool(LIST_TOOL, {})
            accounts_after = await call(client, "list_accounts")

    assert result.is_error is True
    structured = result.structured_content
    assert structured is not None and structured["error"] == "source_unavailable"
    assert structured["items"] == []
    states = {entry["source_kind"]: entry["status"] for entry in structured["accounts"]}
    assert states == {"fixture_mail": "current", MESSENGER: "stale"}
    assert {entry["source_kind"] for entry in accounts_after["accounts"]} == {
        "fixture_mail",
        MESSENGER,
    }


@pytest.mark.anyio
async def test_conversations_and_messages_that_cannot_be_understood_are_skipped_and_counted(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    document = messenger(
        conversation(
            "dm",
            [OPERATOR, "+15550103"],
            say("ok", "+15550103", "2026-01-05T10:00:00Z"),
            say("no-time", "+15550103", "not a time"),
            {"id": "no-sender", "sent": "2026-01-05T10:00:00Z"},
            "not a message",  # type: ignore[arg-type]
        ),
        {"messages": [say("orphan", "+15550103", "2026-01-05T10:00:00Z")]},
    )
    with publish(start_gate, tmp_path, document) as gate:
        async with agent_client(gate) as client:
            result = await call(client, LIST_TOOL)

    assert len(result["items"]) == 1
    assert result["accounts"][0]["status"] == "incomplete"
    assert result["accounts"][0]["skipped_items"] == "4"


# --- the fixture cast and the hostile corpus --------------------------------------------


@pytest.mark.anyio
async def test_the_signal_stand_in_cast_is_served_as_messenger_items(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    state = tmp_path / "seed"
    assert run_seed(state).returncode == 0
    services = json.loads(CAST.read_text())["services"]
    with serve("signal", state) as signal_url, serve("whatsapp", state) as whatsapp_url:
        with start_gate(
            accounts={"signal": signal_url, "whatsapp": whatsapp_url},
            kinds={"signal": MESSENGER, "whatsapp": MESSENGER},
            tokens={"default": ["signal", "whatsapp"]},
        ) as gate:
            channels = await observe_gate(gate)
            async with agent_client(gate) as client:
                items = await listed(client)

    # The two accounts have one message each, from two senders with two handles.
    assert texts(items) == ["2026-01-05T18:00Z", "2026-01-06T07:10Z"]
    assert {item["source_kind"] for item in items} == {MESSENGER}
    assert len({item["sender_handle"] for item in items}) == 2
    served: list[str] = []
    for service in ("signal", "whatsapp"):
        cast = services[service]
        served.append(cast["account"])
        for chat in cast["conversations"]:
            served += [chat["id"], *chat["participants"]]
            for message in chat["messages"]:
                served += [message["from"], message["text"]]
    everything: dict[str, str | bytes] = {**channels, **storage_files(gate.db)}
    for name, content in everything.items():
        text = content if isinstance(content, str) else content.decode("utf-8", "replace")
        assert not any(value in text for value in served), name


@pytest.mark.anyio
async def test_no_corpus_string_reaches_any_channel_from_a_hostile_messenger(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish_both(
        start_gate, tmp_path, hostile_mail_fixture(), hostile_messenger_fixture()
    ) as gate:
        channels = await observe_gate(gate)
        async with agent_client(gate) as client:
            items = await listed(client)
            by_kind = await call(client, GROUP_TOOL, group_by="source_kind")
            by_thread = await call(client, GROUP_TOOL, group_by="thread_handle")
    files = storage_files(gate.db)

    chats = [item for item in items if item["source_kind"] == MESSENGER]
    assert len(chats) == len(CORPUS)
    assert len(items) == 2 * len(CORPUS)
    assert by_kind["groups"] == [
        {"key": "fixture_mail", "count": str(len(CORPUS))},
        {"key": MESSENGER, "count": str(len(CORPUS))},
    ]
    assert len(by_thread["groups"]) == 2 * len(CORPUS)
    # Values that name no permitted status are `unknown`, whatever the source put there.
    assert {item["read_status"] for item in chats} <= {"read", "unread", "unknown"}
    assert all(item["has_attachments"] for item in chats)
    assert {"call", "tools", "refusal", "bad-arguments", "unknown-tool", "log"} <= set(channels)
    assert_no_corpus(channels)
    assert_no_corpus(files)
    assert b"evil.test" not in b"".join(files.values())


@pytest.mark.anyio
async def test_every_tool_schema_stays_closed_with_both_source_kinds(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    mails, chats = two_sources()
    with publish_both(start_gate, tmp_path, mails, chats) as gate:
        async with agent_client(gate) as client:
            tools = (await client.list_tools()).tools

    assert [tool.name for tool in tools] == ALL_TOOLS
    for tool in tools:
        assert tool.output_schema is not None
        assert closed_schema_problems(tool.output_schema) == []
    listing = next(tool for tool in tools if tool.name == LIST_TOOL)
    schema = json.dumps(listing.output_schema)
    assert MESSENGER in schema and "fixture_mail" in schema
    assert "messenger" in (listing.description or "")
