"""Acceptance tests of filters, sorting, grouping, counting and paging (ADR 0006).

Everything goes through MCP tool calls from a real client. The gate's clock is pinned
to `NOW`, so the 30-day window is the same on every day; fixture mail is dated
relative to it. One test reaches below the seam on purpose, marked: whether
out-of-window mail is kept is a property of the stored rows.
"""

import json
import re
import sqlite3
from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from tests.client import COUNT_TOOL, GROUP_TOOL, LIST_TOOL, agent_client
from tests.conftest import GateStarter, RunningGate
from tests.hostile import CORPUS, assert_no_corpus, hostile_mail_fixture, observe_gate
from tests.test_gate import MINUTE, write_fixture

MINUTE_FORMAT = "%Y-%m-%dT%H:%MZ"
CURSOR = re.compile(r"^[A-Za-z0-9_-]{27}$")
COUNT = re.compile(r"^(0|[1-9][0-9]{0,2}|1000|1000\+)$")
ERRORS = {
    "invalid_arguments",
    "invalid_cursor",
    "unknown_tool",
    "source_unavailable",
    "response_invalid",
    "internal_error",
}
TOKEN_A = "token-a-0123456789abcdefghij"
TOKEN_B = "token-b-0123456789abcdefghij"


def at(when: str) -> datetime:
    """A minute in the gate's own format, such as 2026-01-01T00:00Z."""
    return datetime.strptime(when, MINUTE_FORMAT)


def mail(
    label: str,
    sender: str,
    received: str,
    *,
    read: bool | None = None,
    attachments: bool = False,
    thread: str | None = None,
) -> dict[str, object]:
    message: dict[str, object] = {
        "id": label,
        "from": f"{sender}@example.test",
        "received": received,
        "subject": f"Subject {label}",
        "body": f"Body {label}",
    }
    if thread is not None:
        message["thread"] = thread
    if read is not None:
        message["read"] = read
    if attachments:
        message["attachments"] = [{"filename": f"{label}.pdf"}]
    return message


def mailbox(messages: Sequence[dict[str, object]]) -> dict[str, object]:
    return {"account": "alice@example.test", "messages": list(messages)}


def triage_mailbox() -> dict[str, object]:
    """Six items in the window and one older than it, labelled m1 to m6 by time."""
    return mailbox(
        [
            mail("m1", "bob", "2026-01-05T09:00:59Z", read=True, attachments=True, thread="t1"),
            mail("m2", "bob", "2026-01-05T10:30:00Z", read=False, thread="t1"),
            mail("m3", "carol", "2026-01-06T08:00:00Z", thread="t2"),
            mail("m4", "erin", "2026-01-07T07:15:00Z", read=False, attachments=True, thread="t3"),
            mail("m5", "carol", "2026-01-08T16:45:00Z", read=True, thread="t2"),
            mail("m6", "dave", "2026-01-09T11:59:00Z", read=False, thread="t4"),
            mail("old", "bob", "2025-12-01T10:00:00Z", read=False, attachments=True, thread="t1"),
        ]
    )


RECEIVED = {
    "m1": "2026-01-05T09:00Z",
    "m2": "2026-01-05T10:30Z",
    "m3": "2026-01-06T08:00Z",
    "m4": "2026-01-07T07:15Z",
    "m5": "2026-01-08T16:45Z",
    "m6": "2026-01-09T11:59Z",
}
LABELS = {received: label for label, received in RECEIVED.items()}


async def call(client: Client, tool: str, **arguments: object) -> dict[str, Any]:
    """One tool call; whatever it returns has the shape of a fixed result."""
    result = await client.call_tool(tool, arguments)
    assert result.content == []
    structured = result.structured_content
    assert isinstance(structured, dict)
    assert result.is_error == (structured["error"] != "none")
    return structured


def labels(structured: dict[str, Any]) -> list[str]:
    assert structured["error"] == "none"
    return [LABELS[item["received"]] for item in structured["items"]]


def item_handles(structured: dict[str, Any]) -> list[str]:
    return [item["item_handle"] for item in structured["items"]]


def publish(
    start_gate: GateStarter, tmp_path: Path, fixture: dict[str, object], **options: Any
) -> AbstractContextManager[RunningGate]:
    """Start a gate whose one mail source is `fixture`."""
    return start_gate(mail_fixture=write_fixture(tmp_path / "mail.json", fixture), **options)


# --- filters ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_items_are_filtered_by_each_permitted_field(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, triage_mailbox()) as gate:
        async with agent_client(gate) as client:
            everything = await call(client, LIST_TOOL)
            by_label = {LABELS[i["received"]]: i for i in everything["items"]}

            async def filtered(**filters: object) -> list[str]:
                return sorted(labels(await call(client, LIST_TOOL, **filters)))

            assert await filtered() == ["m1", "m2", "m3", "m4", "m5", "m6"]
            assert await filtered(read_status="unread") == ["m2", "m4", "m6"]
            assert await filtered(read_status="read") == ["m1", "m5"]
            assert await filtered(read_status="unknown") == ["m3"]
            assert await filtered(has_attachments=True) == ["m1", "m4"]
            assert await filtered(has_attachments=False) == ["m2", "m3", "m5", "m6"]
            # Both ends of a range are inclusive, and times are compared by the minute.
            assert await filtered(received_from=RECEIVED["m3"]) == ["m3", "m4", "m5", "m6"]
            assert await filtered(received_to=RECEIVED["m3"]) == ["m1", "m2", "m3"]
            assert await filtered(received_to=RECEIVED["m1"]) == ["m1"]
            assert await filtered(received_from=RECEIVED["m3"], received_to=RECEIVED["m3"]) == [
                "m3"
            ]
            assert await filtered(received_from=RECEIVED["m6"], received_to=RECEIVED["m1"]) == []
            bob = by_label["m1"]["sender_handle"]
            carol = by_label["m3"]["sender_handle"]
            assert await filtered(sender_handle=bob) == ["m1", "m2"]
            assert await filtered(sender_handle=carol) == ["m3", "m5"]
            assert await filtered(thread_handle=by_label["m1"]["thread_handle"]) == ["m1", "m2"]
            assert await filtered(thread_handle=by_label["m6"]["thread_handle"]) == ["m6"]
            account = by_label["m1"]["account_handle"]
            assert await filtered(account_handle=account) == ["m1", "m2", "m3", "m4", "m5", "m6"]
            # A handle that names nothing here is answered as an empty set, like any other.
            assert await filtered(account_handle="a" * 24) == []
            assert await filtered(sender_handle="a" * 24) == []
            # Filters combine with AND.
            assert await filtered(sender_handle=bob, read_status="unread") == ["m2"]
            assert await filtered(
                sender_handle=carol, has_attachments=False, received_from=RECEIVED["m4"]
            ) == ["m5"]
            assert await filtered(read_status="unread", has_attachments=True) == ["m4"]
            # An unset filter, sent as null, filters nothing.
            assert await filtered(read_status=None, has_attachments=None) == [
                "m1",
                "m2",
                "m3",
                "m4",
                "m5",
                "m6",
            ]


@pytest.mark.anyio
async def test_count_and_group_apply_the_same_filters(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, triage_mailbox()) as gate:
        async with agent_client(gate) as client:
            bob = (await call(client, LIST_TOOL, received_to=RECEIVED["m1"]))["items"][0]
            for filters, expected in (
                ({}, "6"),
                ({"read_status": "unread"}, "3"),
                ({"has_attachments": True}, "2"),
                ({"received_from": RECEIVED["m3"]}, "4"),
                ({"received_to": RECEIVED["m3"]}, "3"),
                ({"sender_handle": bob["sender_handle"]}, "2"),
                ({"thread_handle": bob["thread_handle"], "read_status": "unread"}, "1"),
                ({"account_handle": bob["account_handle"]}, "6"),
                ({"account_handle": "b" * 24}, "0"),
                ({"read_status": "read", "has_attachments": True}, "1"),
            ):
                counted = await call(client, COUNT_TOOL, **filters)
                assert (counted["error"], counted["count"]) == ("none", expected), filters
                grouped = await call(client, GROUP_TOOL, group_by="read_status", **filters)
                total = sum(int(group["count"]) for group in grouped["groups"])
                assert str(total) == expected, filters


# --- sorting ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_each_fixed_sort_order_is_applied(start_gate: GateStarter, tmp_path: Path) -> None:
    with publish(start_gate, tmp_path, triage_mailbox()) as gate:
        async with agent_client(gate) as client:
            orders = {
                "received_desc": ["m6", "m5", "m4", "m3", "m2", "m1"],
                "received_asc": ["m1", "m2", "m3", "m4", "m5", "m6"],
                # unread, then unknown, then read; newest first within each
                "unread_first": ["m6", "m4", "m2", "m3", "m5", "m1"],
                # with attachments, then without; newest first within each
                "attachments_first": ["m4", "m1", "m6", "m5", "m3", "m2"],
            }
            for order, expected in orders.items():
                assert labels(await call(client, LIST_TOOL, sort=order)) == expected, order
            # The default is the newest first.
            assert labels(await call(client, LIST_TOOL)) == orders["received_desc"]
            # A sort applies within the filters.
            oldest_unread = await call(client, LIST_TOOL, read_status="unread", sort="received_asc")
            assert labels(oldest_unread) == ["m2", "m4", "m6"]
            oldest_with_attachment = await call(
                client, LIST_TOOL, has_attachments=True, read_status="unread", sort="received_asc"
            )
            assert labels(oldest_with_attachment) == ["m4"]

            for order in ("relevance", "score", "best_match", "RECEIVED_DESC", "", "sender"):
                refused = await call(client, LIST_TOOL, sort=order)
                assert refused["error"] == "invalid_arguments", order


# --- grouping and counting --------------------------------------------------------------


@pytest.mark.anyio
async def test_items_are_grouped_with_counts_by_sender_thread_and_each_enum_field(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, triage_mailbox()) as gate:
        async with agent_client(gate) as client:
            by_label = {LABELS[i["received"]]: i for i in (await call(client, LIST_TOOL))["items"]}

            async def groups(group_by: str, **filters: object) -> list[tuple[str, str]]:
                grouped = await call(client, GROUP_TOOL, group_by=group_by, **filters)
                assert grouped["error"] == "none"
                assert grouped["next_cursor"] is None
                return [(group["key"], group["count"]) for group in grouped["groups"]]

            by_sender = await groups("sender_handle")
            assert [count for _, count in by_sender] == ["2", "2", "1", "1"]
            assert {key: count for key, count in by_sender} == {
                by_label["m1"]["sender_handle"]: "2",
                by_label["m3"]["sender_handle"]: "2",
                by_label["m4"]["sender_handle"]: "1",
                by_label["m6"]["sender_handle"]: "1",
            }
            by_thread = await groups("thread_handle")
            assert {key: count for key, count in by_thread} == {
                by_label["m1"]["thread_handle"]: "2",
                by_label["m3"]["thread_handle"]: "2",
                by_label["m4"]["thread_handle"]: "1",
                by_label["m6"]["thread_handle"]: "1",
            }
            # Largest first, then by key.
            assert by_sender[:2] == sorted(by_sender[:2])
            assert by_sender[2:] == sorted(by_sender[2:])
            assert await groups("read_status") == [("unread", "3"), ("read", "2"), ("unknown", "1")]
            assert await groups("source_kind") == [("fixture_mail", "6")]
            # "How many unread items are there per thread", and with attachments only.
            unread_per_thread = await groups("thread_handle", read_status="unread")
            assert sorted(count for _, count in unread_per_thread) == ["1", "1", "1"]
            # Equal counts are ordered by key.
            assert await groups("read_status", has_attachments=True) == [
                ("read", "1"),
                ("unread", "1"),
            ]
            assert await groups("read_status", account_handle="c" * 24) == []

            for group_by in ("subject", "sender", "received", "has_attachments", "", None):
                refused = await call(client, GROUP_TOOL, group_by=group_by)
                assert refused["error"] == "invalid_arguments", group_by
            missing = await call(client, GROUP_TOOL)
            assert missing["error"] == "invalid_arguments"


# --- paging -----------------------------------------------------------------------------


def numbered_mailbox(count: int, senders: int = 60) -> dict[str, object]:
    start = at("2026-01-01T00:00Z")
    return mailbox(
        [
            mail(
                f"n{index}",
                f"s{index % senders}",
                (start + timedelta(minutes=index)).strftime(MINUTE_FORMAT),
            )
            for index in range(count)
        ]
    )


@pytest.mark.anyio
async def test_a_response_holds_at_most_fifty_items_and_a_cursor_leads_on(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, numbered_mailbox(125)) as gate:
        async with agent_client(gate) as client:
            first = await call(client, LIST_TOOL)
            assert len(first["items"]) == 50
            assert CURSOR.match(first["next_cursor"])
            second = await call(client, LIST_TOOL, cursor=first["next_cursor"])
            assert len(second["items"]) == 50
            third = await call(client, LIST_TOOL, cursor=second["next_cursor"])
            assert len(third["items"]) == 25
            assert third["next_cursor"] is None

            handles = item_handles(first) + item_handles(second) + item_handles(third)
            assert len(set(handles)) == 125
            received = [i["received"] for page in (first, second, third) for i in page["items"]]
            assert received == sorted(received, reverse=True)

            # The same walk in the other order, and with a smaller page size.
            ascending = await call(client, LIST_TOOL, sort="received_asc", limit=7)
            assert len(ascending["items"]) == 7
            assert [i["received"] for i in ascending["items"]] == sorted(received)[:7]
            resumed = await call(
                client, LIST_TOOL, sort="received_asc", limit=20, cursor=ascending["next_cursor"]
            )
            assert [i["received"] for i in resumed["items"]] == sorted(received)[7:27]

            # Nothing asks for more than 50 at a time.
            for limit in (0, -1, 51, 1000, "5", 5.5, True, None):
                refused = await call(client, LIST_TOOL, limit=limit)
                assert refused["error"] == "invalid_arguments", limit


@pytest.mark.anyio
async def test_the_page_boundary_is_exact(start_gate: GateStarter, tmp_path: Path) -> None:
    with publish(start_gate, tmp_path, numbered_mailbox(50)) as gate:
        async with agent_client(gate) as client:
            page = await call(client, LIST_TOOL)
            assert len(page["items"]) == 50
            assert page["next_cursor"] is None
    with publish(start_gate, tmp_path, numbered_mailbox(51)) as gate:
        async with agent_client(gate) as client:
            page = await call(client, LIST_TOOL)
            assert len(page["items"]) == 50
            last = await call(client, LIST_TOOL, cursor=page["next_cursor"])
            assert len(last["items"]) == 1
            assert last["next_cursor"] is None
            short = await call(client, LIST_TOOL, limit=1)
            assert len(short["items"]) == 1
            assert CURSOR.match(short["next_cursor"])
    with publish(start_gate, tmp_path, numbered_mailbox(0)) as gate:
        async with agent_client(gate) as client:
            empty = await call(client, LIST_TOOL)
            # An empty inbox is a success whose account is current, unlike an outage.
            assert (empty["error"], empty["items"], empty["next_cursor"]) == ("none", [], None)
            assert [account["status"] for account in empty["accounts"]] == ["current"]
            counted = await call(client, COUNT_TOOL)
            assert (counted["error"], counted["count"]) == ("none", "0")
            assert counted["accounts"] == empty["accounts"]


@pytest.mark.anyio
async def test_groups_are_paged_like_items(start_gate: GateStarter, tmp_path: Path) -> None:
    fixture = numbered_mailbox(120)  # 60 senders, two items each
    # One sender writes five more, so the largest group is not one of the 2-item ones.
    fixture["messages"] = [
        *fixture["messages"],  # type: ignore[misc]
        *[mail(f"big{n}", "big", "2026-01-02T00:00:00Z") for n in range(5)],
    ]
    with publish(start_gate, tmp_path, fixture) as gate:
        async with agent_client(gate) as client:
            first = await call(client, GROUP_TOOL, group_by="sender_handle")
            assert len(first["groups"]) == 50
            assert first["groups"][0]["count"] == "5"
            second = await call(
                client, GROUP_TOOL, group_by="sender_handle", cursor=first["next_cursor"]
            )
            assert len(second["groups"]) == 11
            assert second["next_cursor"] is None
            groups = first["groups"] + second["groups"]
            assert len({group["key"] for group in groups}) == 61
            assert [group["count"] for group in groups] == ["5"] + ["2"] * 60
            small = await call(client, GROUP_TOOL, group_by="sender_handle", limit=3)
            assert [g["key"] for g in small["groups"]] == [g["key"] for g in groups[:3]]
            assert CURSOR.match(small["next_cursor"])


# --- count cap --------------------------------------------------------------------------


@pytest.mark.anyio
async def test_counts_above_one_thousand_are_reported_as_1000_plus(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    start = at("2026-01-01T00:00Z")

    def minute_of(index: int) -> str:
        return (start + timedelta(minutes=index)).strftime(MINUTE_FORMAT)

    fixture = mailbox([mail(f"c{i}", "bulk", minute_of(i), read=False) for i in range(1001)])
    with publish(start_gate, tmp_path, fixture) as gate:
        async with agent_client(gate) as client:
            for last, expected in ((0, "1"), (998, "999"), (999, "1000"), (1000, "1000+")):
                counted = await call(client, COUNT_TOOL, received_to=minute_of(last))
                assert (counted["error"], counted["count"]) == ("none", expected), last
            everything = await call(client, COUNT_TOOL)
            assert (everything["error"], everything["count"]) == ("none", "1000+")
            grouped = await call(client, GROUP_TOOL, group_by="read_status")
            assert grouped["groups"] == [{"key": "unread", "count": "1000+"}]
            # A page is still 50 items. Walking every page does reveal a total above the
            # cap: that side channel is bounded by the page size, not closed (ADR 0001, b).
            listed = await call(client, LIST_TOOL)
            assert len(listed["items"]) == 50


@pytest.mark.anyio
async def test_groups_above_the_cap_are_ordered_by_key_not_by_size(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    fixture = mailbox(
        [
            *[mail(f"a{i}", "alpha", "2026-01-02T00:00:00Z") for i in range(1001)],
            *[mail(f"b{i}", "bravo", "2026-01-03T00:00:00Z") for i in range(1007)],
            *[mail(f"c{i}", "charlie", "2026-01-04T00:00:00Z") for i in range(3)],
        ]
    )
    with publish(start_gate, tmp_path, fixture) as gate:
        async with agent_client(gate) as client:
            grouped = await call(client, GROUP_TOOL, group_by="sender_handle")
    groups = [(group["key"], group["count"]) for group in grouped["groups"]]
    assert [count for _, count in groups] == ["1000+", "1000+", "3"]
    assert groups[:2] == sorted(groups[:2])


# --- cursors ----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_cursor_is_invalid_for_another_token_identity_and_valid_again_for_its_own(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    fixture = write_fixture(tmp_path / "mail.json", numbered_mailbox(125))
    with start_gate("shared.db", mail_fixture=fixture, token=TOKEN_A) as gate:
        async with agent_client(gate) as client:
            first = await call(client, LIST_TOOL)
            group_first = await call(client, GROUP_TOOL, group_by="sender_handle", limit=5)
            second = await call(client, LIST_TOOL, cursor=first["next_cursor"])
        cursor = first["next_cursor"]
        group_cursor = group_first["next_cursor"]
    with start_gate("shared.db", mail_fixture=fixture, token=TOKEN_B) as gate:
        async with agent_client(gate) as client:
            refused = await call(client, LIST_TOOL, cursor=cursor)
            assert refused == {
                "error": "invalid_cursor",
                "accounts": [],
                "items": [],
                "next_cursor": None,
            }
            refused_group = await call(
                client, GROUP_TOOL, group_by="sender_handle", cursor=group_cursor
            )
            assert refused_group["error"] == "invalid_cursor"
            # The other identity can still page through its own cursors.
            own = await call(client, LIST_TOOL)
            assert own["items"] == first["items"]
            followed = await call(client, LIST_TOOL, cursor=own["next_cursor"])
            assert followed["items"] == second["items"]
            # What it was handed is not what the first identity was handed.
            assert own["next_cursor"] != cursor
    with start_gate("shared.db", mail_fixture=fixture, token=TOKEN_A) as gate:
        async with agent_client(gate) as client:
            # Cursors survive a restart of the service for the identity they were issued to.
            again = await call(client, LIST_TOOL, cursor=cursor)
            assert again["items"] == second["items"]


@pytest.mark.anyio
async def test_a_cursor_is_invalid_for_any_other_query_and_for_forgeries(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, numbered_mailbox(125)) as gate:
        async with agent_client(gate) as client:
            page = await call(client, LIST_TOOL, read_status="unknown", limit=10)
            cursor = page["next_cursor"]
            assert (await call(client, LIST_TOOL, read_status="unknown", cursor=cursor))[
                "error"
            ] == "none"

            other_queries: list[tuple[str, dict[str, object]]] = [
                (LIST_TOOL, {"read_status": "unknown", "sort": "received_asc"}),
                (LIST_TOOL, {"read_status": "unread"}),
                (LIST_TOOL, {}),
                (LIST_TOOL, {"read_status": "unknown", "has_attachments": False}),
                (LIST_TOOL, {"read_status": "unknown", "received_from": "2026-01-01T00:00Z"}),
                (GROUP_TOOL, {"read_status": "unknown", "group_by": "sender_handle"}),
            ]
            for tool, arguments in other_queries:
                refused = await call(client, tool, cursor=cursor, **arguments)
                assert refused["error"] == "invalid_cursor", (tool, arguments)

            forged = [
                "A" * 27,
                "0" * 27,
                cursor[:-1] + ("A" if cursor[-1] != "A" else "B"),
                cursor[1:] + cursor[0],
                "AAAAAAAAAAAAAAAAAAAAAAAAAAA".replace("A", "_"),
            ]
            for value in forged:
                refused = await call(client, LIST_TOOL, read_status="unknown", cursor=value)
                assert refused["error"] == "invalid_cursor", value
            # A cursor of the wrong shape is not a cursor at all.
            for value in ("", "short", cursor + "A", "!" * 27, 12345, ["x"], {"a": "b"}):
                refused = await call(client, LIST_TOOL, read_status="unknown", cursor=value)
                assert refused["error"] == "invalid_arguments", value


@pytest.mark.anyio
async def test_a_cursor_reveals_nothing_about_content(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    """The same position gives the same cursor whatever the items are, for the same caller."""
    other = numbered_mailbox(125)
    other["account"] = "dave@example.test"
    other["messages"] = [
        mail(f"x{i}", f"other{i}", f"2026-01-0{1 + i % 9}T0{i % 10}:00:00Z", read=bool(i % 2))
        for i in range(125)
    ]
    cursors: list[str] = []
    for index, fixture in enumerate((numbered_mailbox(125), other)):
        mail_path = write_fixture(tmp_path / f"mail{index}.json", fixture)
        with start_gate("shared.db", mail_fixture=mail_path) as gate:
            async with agent_client(gate) as client:
                page = await call(client, LIST_TOOL)
                cursors.append(page["next_cursor"])
    assert CURSOR.match(cursors[0])
    assert cursors[0] == cursors[1]


# --- the window -------------------------------------------------------------------------


def window_mailbox() -> dict[str, object]:
    return mailbox(
        [
            mail("early", "bob", "2025-12-11T11:59:59Z"),
            mail("first", "bob", "2025-12-11T12:00:00Z"),
            mail("recent", "bob", "2026-01-09T08:00:00Z"),
            mail("now", "bob", "2026-01-10T12:00:30Z"),
            mail("ahead", "bob", "2026-01-10T12:01:00Z"),
            mail("far-ahead", "bob", "2026-02-20T12:00:00Z"),
            mail("ancient", "bob", "2025-01-01T00:00:00Z"),
            mail("offset", "bob", "2025-12-11T13:30:00+01:00"),
        ]
    )


@pytest.mark.anyio
async def test_mail_outside_the_rolling_thirty_day_window_is_not_served(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, window_mailbox()) as gate:
        async with agent_client(gate) as client:
            listed = await call(client, LIST_TOOL, sort="received_asc")
            assert [i["received"] for i in listed["items"]] == [
                "2025-12-11T12:00Z",
                "2025-12-11T12:30Z",
                "2026-01-09T08:00Z",
                "2026-01-10T12:00Z",
            ]
            assert (await call(client, COUNT_TOOL))["count"] == "4"
            grouped = await call(client, GROUP_TOOL, group_by="read_status")
            assert grouped["groups"] == [{"key": "unknown", "count": "4"}]
            # A filter cannot reach back past the window or forward past now.
            wide = await call(client, COUNT_TOOL, received_from="2000-01-01T00:00Z")
            assert wide["count"] == "4"
            wide = await call(client, COUNT_TOOL, received_to="2099-01-01T00:00Z")
            assert wide["count"] == "4"
            only_old = await call(client, COUNT_TOOL, received_to="2025-12-11T11:59Z")
            assert only_old["count"] == "0"
    # A minute later the oldest item of the window has left it.
    with publish(start_gate, tmp_path, window_mailbox(), now="2026-01-10T12:01:00Z") as gate:
        async with agent_client(gate) as client:
            listed = await call(client, LIST_TOOL, sort="received_asc")
            assert [i["received"] for i in listed["items"]] == [
                "2025-12-11T12:30Z",
                "2026-01-09T08:00Z",
                "2026-01-10T12:00Z",
                "2026-01-10T12:01Z",
            ]


@pytest.mark.anyio
async def test_mail_outside_the_window_is_not_ingested(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    """Below the seam on purpose: not being ingested is a property of the stored rows."""
    with publish(start_gate, tmp_path, window_mailbox()) as gate:
        async with agent_client(gate) as client:
            await call(client, LIST_TOOL)
    with sqlite3.connect(gate.db) as database:
        stored = [row[0] for row in database.execute("select received from items order by 1")]
        item_handles = database.execute("select count(*) from handles where kind = 'item'")
        minted = item_handles.fetchone()[0]
    assert stored == [
        "2025-12-11T12:00Z",
        "2025-12-11T12:30Z",
        "2026-01-09T08:00Z",
        "2026-01-10T12:00Z",
    ]
    # No handle was minted for mail that was never kept.
    assert minted == 4


@pytest.mark.anyio
async def test_stored_items_of_another_account_are_dropped_when_they_leave_the_window(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    """Below the seam on purpose: what the database keeps is not visible to an agent."""
    first = mailbox([mail("a1", "bob", "2026-01-05T09:00:00Z")])
    second = mailbox([mail("b1", "bob", "2026-02-12T09:00:00Z")])
    second["account"] = "dave@example.test"
    first_path = write_fixture(tmp_path / "first.json", first)
    second_path = write_fixture(tmp_path / "second.json", second)
    with start_gate("one.db", mail_fixture=first_path) as gate:
        async with agent_client(gate) as client:
            await call(client, LIST_TOOL)
    with sqlite3.connect(gate.db) as database:
        assert database.execute("select count(*) from items").fetchone()[0] == 1
    with start_gate("one.db", mail_fixture=second_path, now="2026-02-14T12:00:00Z") as gate:
        async with agent_client(gate) as client:
            await call(client, LIST_TOOL)
    with sqlite3.connect(gate.db) as database:
        stored = [row[0] for row in database.execute("select received from items")]
    assert stored == ["2026-02-12T09:00Z"]


# --- timestamps -------------------------------------------------------------------------


@pytest.mark.anyio
async def test_timestamps_are_minute_precision_in_and_out(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, triage_mailbox()) as gate:
        async with agent_client(gate) as client:
            listed = await call(client, LIST_TOOL)
            assert all(MINUTE.match(item["received"]) for item in listed["items"])
            for value in (
                "２０２６-01-06T08:00Z",  # full-width digits
                "2026-01-06T٠٨:00Z",  # Arabic-Indic digits
                "2026-01-06T08:00:30Z",
                "2026-01-06T08:00:00Z",
                "2026-01-06T08:00",
                "2026-01-06T08:00+01:00",
                "2026-01-06",
                "2026-13-06T08:00Z",
                "2026-01-06T25:00Z",
                "2026-02-30T08:00Z",
                "yesterday",
                "",
                20260106,
            ):
                for name in ("received_from", "received_to"):
                    refused = await call(client, COUNT_TOOL, **{name: value})
                    assert refused["error"] == "invalid_arguments", (name, value)


# --- inputs -----------------------------------------------------------------------------


@pytest.mark.anyio
async def test_inputs_are_closed_and_there_is_no_text_or_semantic_query(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with publish(start_gate, tmp_path, triage_mailbox()) as gate:
        async with agent_client(gate) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            filters = {
                "read_status",
                "received_from",
                "received_to",
                "has_attachments",
                "sender_handle",
                "thread_handle",
                "account_handle",
                # The semantic fields are closed vocabularies too: filtering on one is not
                # a text query.
                "topic",
                "reply_needed",
                "action_requested",
                "deadline_from",
                "deadline_to",
                "classification_status",
            }
            assert set(tools[COUNT_TOOL].input_schema["properties"]) == filters
            assert set(tools[LIST_TOOL].input_schema["properties"]) == filters | {
                "sort",
                "limit",
                "cursor",
            }
            assert set(tools[GROUP_TOOL].input_schema["properties"]) == filters | {
                "group_by",
                "limit",
                "cursor",
            }
            # Every parameter is an enum, an anchored pattern, a boolean or a bounded number.
            for tool in tools.values():
                assert tool.input_schema["additionalProperties"] is False
                text = json.dumps(tool.input_schema)
                assert '"type": "object"' in text and '"format"' not in text

            for name in ("query", "text", "search", "q", "semantic", "topic", "subject", "body"):
                for tool_name in tools:
                    arguments: dict[str, object] = {name: "unread invoices"}
                    if tool_name == GROUP_TOOL:
                        arguments["group_by"] = "read_status"
                    refused = await call(client, tool_name, **arguments)
                    assert refused["error"] == "invalid_arguments", (tool_name, name)

            bad_values: dict[str, list[object]] = {
                "read_status": ["READ", "seen", "", 1, True, ["read"], {"is": "read"}],
                "has_attachments": ["yes", "true", 1, 0, [True], {}],
                "sender_handle": [
                    "",
                    "a" * 23,
                    "a" * 25,
                    "A" * 24,
                    "a" * 23 + "1",
                    "bob@example.test",
                ],
                "thread_handle": ["", "a" * 25, "1" * 24, 7],
                "account_handle": ["", "a" * 25, "!" * 24],
            }
            for name, values in bad_values.items():
                for value in values:
                    refused = await call(client, COUNT_TOOL, **{name: value})
                    assert refused["error"] == "invalid_arguments", (name, value)


# --- the hostile corpus -----------------------------------------------------------------


@pytest.mark.anyio
async def test_hostile_mail_reaches_no_channel_of_any_query_tool(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    fixture = write_fixture(tmp_path / "hostile.json", hostile_mail_fixture())
    with start_gate(mail_fixture=fixture) as gate:
        channels = await observe_gate(gate)
        async with agent_client(gate) as client:
            per_sender = await call(client, GROUP_TOOL, group_by="sender_handle")
            per_thread = await call(client, GROUP_TOOL, group_by="thread_handle")
            per_status = await call(client, GROUP_TOOL, group_by="read_status")
            counted = await call(client, COUNT_TOOL)
    for name, response in (
        ("sender", per_sender),
        ("thread", per_thread),
        ("status", per_status),
        ("count", counted),
    ):
        channels[f"result {name}"] = json.dumps(response)

    # Every group is a handle or a value of an enum, whatever the mail held.
    handle = re.compile(r"^[a-z2-7]{24}$")
    assert all(handle.match(group["key"]) for group in per_sender["groups"] + per_thread["groups"])
    assert {group["key"] for group in per_status["groups"]} <= {"read", "unread", "unknown"}
    assert counted["count"] == str(len(CORPUS))
    for tool in (LIST_TOOL, COUNT_TOOL, GROUP_TOOL):
        assert any(name.startswith(f"call-{tool}") for name in channels)
    assert len([name for name in channels if name.startswith("refused ")]) > 100
    assert any(name.startswith("foreign cursor") for name in channels)
    assert_no_corpus(channels)


@pytest.mark.anyio
async def test_error_results_of_every_tool_are_fixed_values(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    fixture = write_fixture(tmp_path / "hostile.json", hostile_mail_fixture())
    with start_gate(mail_fixture=fixture) as gate:
        async with agent_client(gate) as client:
            seen: set[str] = set()
            for tool, arguments in (
                (LIST_TOOL, {"sort": CORPUS["injection"]}),
                (COUNT_TOOL, {"sender_handle": CORPUS["tool_call"]}),
                (GROUP_TOOL, {"group_by": CORPUS["json_break"]}),
                (LIST_TOOL, {"cursor": CORPUS["long"]}),
                (LIST_TOOL, {"cursor": "A" * 27}),
                (CORPUS["tool_call"], {}),
            ):
                result = await client.call_tool(tool, arguments)
                structured = result.structured_content
                assert isinstance(structured, dict)
                assert result.is_error is True
                assert result.content == []
                assert structured["error"] in ERRORS
                assert set(structured) <= {
                    "error",
                    "accounts",
                    "items",
                    "groups",
                    "count",
                    "next_cursor",
                }
                assert structured.get("accounts", []) == []
                seen.add(structured["error"])
                assert_no_corpus({"result": result.model_dump_json(by_alias=True)})
            assert seen == {"invalid_arguments", "invalid_cursor", "unknown_tool"}
