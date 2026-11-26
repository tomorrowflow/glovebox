"""Acceptance tests of the read-only mailbox.org mail source (ADR 0003, 0004, 0006).

Everything goes through MCP tool calls from a real client against a running gate whose
`mailbox_org_mail` account reads an IMAP server stand-in (`tests/imap_standin.py`) over a
real socket. The stand-in records every command it receives and keeps message flags the way
a server does, so "reading changes nothing" is a fact about commands and about the mailbox,
not a promise. Tests that reach below the seam say so: the TLS settings of the connection,
the attachment rule over BODYSTRUCTURE and the read cap are not visible through a tool call.
What mailbox.org itself does is checked by the operator (docs/mailbox-org-mail.md).
"""

import dataclasses
import json
import ssl
import subprocess
import sys
import threading
import time
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from imapclient import IMAPClient

from glovebox import imapmail
from glovebox.access import Access
from glovebox.credentials import Secret
from tests.client import ACCOUNTS_TOOL, COUNT_TOOL, GROUP_TOOL, LIST_TOOL, agent_client
from tests.conftest import GateStarter, RunningGate, clean_environment, free_port, operator
from tests.hostile import CORPUS, assert_no_corpus, observe_gate
from tests.imap_standin import (
    FOLDER,
    PASSWORD,
    PEEKING_ITEM,
    READ_COMMANDS,
    USER,
    ImapStandIn,
    Mail,
    fetch_items,
    hostile_inbox,
    imap_serving,
    mail,
)
from tests.test_classifier import GOOD, GOOD_SERVED, Setup
from tests.test_failures import account_of
from tests.test_gate import storage_files
from tests.test_queries import call

ACCOUNT = "mail"
KIND = "mailbox_org_mail"
FIRST_SYNC = "2026-01-10T12:00Z"
DEADLINE_VAR = "GLOVEBOX_MAILBOX_READ_DEADLINE_SECONDS"
LOGIN = f"{USER}\n{PASSWORD}\n"
WINDOW_START = datetime(2025, 12, 11, 12, 0, tzinfo=UTC)


def inbox() -> list[Mail]:
    """Four messages in the window, one of them a reply in Bob's thread, and one that the
    server returns for its day but that is older than the gate's 30 days."""
    return [
        mail(
            1,
            "Bob <bob@example.test>",
            "2026-01-05T18:00:00Z",
            subject="Plans",
            body="Dinner on Friday?",
            read=True,
            attachments=["menu.pdf"],
            message_id="<root@example.test>",
        ),
        mail(
            2,
            "Bob <BOB@example.test>",
            "2026-01-05T18:05:30Z",
            subject="Re: Plans",
            body="Or Saturday.",
            references="<root@example.test> <mid@example.test>",
            in_reply_to="<mid@example.test>",
        ),
        mail(3, "carol@example.test", "2026-01-08T09:30:45+01:00", subject="Hello", body="Hi."),
        mail(4, "dave@example.test", "2026-01-09T08:00:00Z", subject="Bye", body="Bye.", read=True),
        mail(5, "erin@example.test", "2025-12-11T06:00:00Z", subject="Old", body="Old."),
    ]


def add_account(
    tmp_path: Path, url: str, login: str = LOGIN, mode: int = 0o600
) -> subprocess.CompletedProcess[str]:
    """The operator adds a mailbox.org account the way the documentation says."""
    credentials = tmp_path / "mailbox-login"
    credentials.write_text(login)
    credentials.chmod(mode)
    return operator(
        tmp_path / "glovebox.db",
        *("account", "add", ACCOUNT, "--kind", KIND, "--source", url),
        *("--credentials", str(credentials)),
    )


def serving_gate(
    start_gate: GateStarter, tmp_path: Path, standin: ImapStandIn, **extra: Any
) -> AbstractContextManager[RunningGate]:
    """A gate whose mailbox.org account reads the stand-in."""
    added = add_account(tmp_path, standin.url)
    assert added.returncode == 0, added.stderr
    return start_gate(accounts={ACCOUNT: standin.url}, kinds={ACCOUNT: KIND}, **extra)


# --- what an item is ------------------------------------------------------------------------


@pytest.mark.anyio
async def test_mail_of_the_window_is_served_as_items_with_permitted_fields(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with imap_serving(inbox()) as standin:
        with serving_gate(start_gate, tmp_path, standin) as gate:
            async with agent_client(gate) as client:
                listing = await call(client, LIST_TOOL, sort="received_asc")
                counted = await call(client, COUNT_TOOL, read_status="unread")
                grouped = await call(client, GROUP_TOOL, group_by="thread_handle")

    items = listing["items"]
    assert [item["received"] for item in items] == [
        "2026-01-05T18:00Z",
        "2026-01-05T18:05Z",
        "2026-01-08T08:30Z",
        "2026-01-09T08:00Z",
    ]
    assert [item["read_status"] for item in items] == ["read", "unread", "unread", "read"]
    assert [item["has_attachments"] for item in items] == [True, False, False, False]
    assert {item["source_kind"] for item in items} == {KIND}
    # One address is one sender whatever its case; a reply is in the thread it answers.
    assert items[0]["sender_handle"] == items[1]["sender_handle"]
    assert len({item["sender_handle"] for item in items}) == 3
    assert items[0]["thread_handle"] == items[1]["thread_handle"]
    assert len({item["thread_handle"] for item in items}) == 3
    entry = account_of(listing)
    assert (entry["status"], entry["last_sync"], entry["skipped_items"]) == (
        "current",
        FIRST_SYNC,
        "0",
    )
    assert counted["count"] == "2"
    assert sorted(group["count"] for group in grouped["groups"]) == ["1", "1", "2"]


@pytest.mark.anyio
async def test_the_account_reports_that_read_only_is_enforced_by_the_gateway(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with imap_serving(inbox()) as standin:
        with serving_gate(start_gate, tmp_path, standin) as gate:
            async with agent_client(gate) as client:
                accounts = await call(client, ACCOUNTS_TOOL)
                listing = await call(client, LIST_TOOL)

    for result in (accounts, listing):
        (entry,) = result["accounts"]
        assert (entry["source_kind"], entry["read_only_enforcement"]) == (KIND, "gateway")


@pytest.mark.anyio
async def test_subject_and_text_reach_the_classifier_and_the_semantic_fields_are_filled(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    setup = Setup(tmp_path)
    setup.rule("subject-marker", GOOD)
    setup.rule("body-marker", {**GOOD, "topic": "travel"})
    setup.rule("html-marker", {**GOOD, "topic": "other"})
    mails = [
        mail(1, "a@example.test", "2026-01-05T09:00:00Z", subject="subject-marker", body="x"),
        mail(2, "b@example.test", "2026-01-05T09:01:00Z", subject="s", body="a body-marker"),
        mail(
            3,
            "c@example.test",
            "2026-01-05T09:02:00Z",
            html="<p>an html-marker</p><script>subject-marker</script>",
        ),
    ]
    with imap_serving(mails) as standin:
        with serving_gate(start_gate, tmp_path, standin, extra_env=setup.env()) as gate:
            async with agent_client(gate) as client:
                listing = await call(client, LIST_TOOL, sort="received_asc")

    by_topic = [(item["topic"], item["classification_status"]) for item in listing["items"]]
    # The script's text is not served; the third message's script is no part of its text.
    assert by_topic == [
        ("billing", "classified"),
        ("travel", "classified"),
        ("other", "classified"),
    ]
    assert listing["items"][0]["deadline"] == GOOD_SERVED["deadline"]


# --- reading changes nothing ----------------------------------------------------------------


@pytest.mark.anyio
async def test_reading_sends_no_command_that_changes_the_mailbox(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with imap_serving(inbox()) as standin:
        before = standin.flags()
        with serving_gate(start_gate, tmp_path, standin) as gate:
            async with agent_client(gate) as client:
                await call(client, ACCOUNTS_TOOL)
                await call(client, LIST_TOOL)
                await call(client, COUNT_TOOL, read_status="unread")
                await call(client, GROUP_TOOL, group_by="sender_handle")
                for fault in ("fetch_no", "garbage", "auth", "select_no", "fetch_drop"):
                    standin.fault = fault
                    await call(client, LIST_TOOL)
                standin.fault = None
                await call(client, LIST_TOOL)

    assert standin.commands, "the gate sent nothing"
    # Only the commands of a read: nothing that selects for writing, stores, copies, moves,
    # deletes, expunges, appends, creates or renames.
    assert set(standin.commands) <= READ_COMMANDS, standin.commands
    assert "EXAMINE" in standin.commands
    # Every fetch peeks: none can set \Seen, even on a server that would let it.
    assert standin.fetches
    for line in standin.fetches:
        assert all(PEEKING_ITEM.fullmatch(item) for item in fetch_items(line)), line
    assert standin.flags() == before


def test_the_stand_in_would_show_a_read_that_marks_mail_seen() -> None:
    """The check above can fail: read the wrong way, the stand-in changes the mailbox."""
    with imap_serving(inbox()) as standin:
        before = standin.flags()
        client = IMAPClient("127.0.0.1", standin.port, ssl=False, timeout=5)
        client.login(USER, PASSWORD)
        client.select_folder(FOLDER)  # read-write
        client.fetch([2], ["BODY[]"])
        client.logout()

        assert "\\Seen" in standin.flags()[2]
        assert standin.flags() != before
        assert not set(standin.commands) <= READ_COMMANDS
        assert not all(PEEKING_ITEM.fullmatch(item) for item in fetch_items(standin.fetches[0]))


# --- kept current ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_new_mail_and_changes_made_in_the_mailbox_show_up_at_the_next_read(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with imap_serving(inbox()) as standin:
        with serving_gate(start_gate, tmp_path, standin) as gate:
            async with agent_client(gate) as client:
                before = await call(client, COUNT_TOOL)
                unread = await call(client, COUNT_TOOL, read_status="unread")
                standin.mails.append(
                    mail(6, "fred@example.test", "2026-01-10T11:00:00Z", subject="New", body="Hi")
                )
                standin.mails[1].flags.add("\\Seen")  # the operator read it in their mail client
                after = await call(client, COUNT_TOOL)
                still_unread = await call(client, COUNT_TOOL, read_status="unread")
                newest = await call(client, LIST_TOOL)

    assert (before["count"], after["count"]) == ("4", "5")
    assert (unread["count"], still_unread["count"]) == ("2", "2")  # one read, one arrived
    assert newest["items"][0]["received"] == "2026-01-10T11:00Z"
    assert newest["items"][0]["read_status"] == "unread"


@pytest.mark.anyio
async def test_a_gate_restart_keeps_the_handles_of_mail_it_has_seen(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with imap_serving(inbox()) as standin:
        with serving_gate(start_gate, tmp_path, standin) as gate:
            async with agent_client(gate) as client:
                first = await call(client, LIST_TOOL)
        with start_gate(accounts={ACCOUNT: standin.url}, kinds={ACCOUNT: KIND}) as gate:
            async with agent_client(gate) as client:
                second = await call(client, LIST_TOOL)

    assert second["items"] == first["items"]
    assert account_of(second)["account_handle"] == account_of(first)["account_handle"]


# --- failure behavior (ticket 08) -------------------------------------------------------------


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("fault", "status"),
    [
        ("down", "stale"),
        ("bye", "stale"),
        ("select_no", "stale"),
        ("fetch_no", "stale"),
        ("fetch_drop", "stale"),
        ("garbage", "stale"),
        ("auth", "blocked"),
    ],
)
async def test_an_outage_or_a_refusal_is_reported_and_never_an_empty_inbox(
    start_gate: GateStarter, tmp_path: Path, fault: str, status: str
) -> None:
    with imap_serving(inbox()) as standin:
        with serving_gate(start_gate, tmp_path, standin) as gate:
            async with agent_client(gate) as client:
                before = await call(client, LIST_TOOL)
                standin.fault = fault
                down = {
                    tool: await call(client, tool, **arguments)
                    for tool, arguments in (
                        (LIST_TOOL, {}),
                        (COUNT_TOOL, {}),
                        (GROUP_TOOL, {"group_by": "thread_handle"}),
                    )
                }
                standin.fault = None
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
async def test_a_wrong_password_is_blocked_from_the_first_read(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with imap_serving(inbox(), password="another-password-0123456789") as standin:
        with serving_gate(start_gate, tmp_path, standin) as gate:
            async with agent_client(gate) as client:
                result = await call(client, LIST_TOOL)

    assert result["error"] == "source_unavailable" and result["items"] == []
    entry = account_of(result)
    assert (entry["status"], entry["last_sync"]) == ("blocked", None)


@pytest.mark.anyio
async def test_malformed_messages_are_skipped_and_counted_and_the_rest_is_served(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    good = inbox()[:4]
    text = b'("TEXT" "PLAIN" NIL NIL NIL "7BIT" 4 1)'
    when = datetime(2026, 1, 7, 7, 0, tzinfo=UTC)
    broken = [
        Mail(10, b"Subject: no sender\r\n\r\nbody", text, when),
        dataclasses.replace(mail(11, "x@example.test", when.isoformat()), internaldate=b'"nope"'),
        dataclasses.replace(mail(12, "y@example.test", when.isoformat()), structure=b'("TEXT")'),
        Mail(13, b"\xff\xfe\x00 not a message at all", text, when),
        # The library cannot parse the response holding this one, so the batch is retried
        # message by message and only this message is lost.
        dataclasses.replace(mail(14, "z@example.test", when.isoformat()), structure=b"((((("),
    ]
    with imap_serving([*good[:2], *broken, *good[2:]]) as standin:
        with serving_gate(start_gate, tmp_path, standin) as gate:
            async with agent_client(gate) as client:
                listing = await call(client, LIST_TOOL)
                standin.mails = good
                fixed = await call(client, LIST_TOOL)

    assert len(listing["items"]) == 4
    entry = account_of(listing)
    assert (entry["status"], entry["skipped_items"]) == ("incomplete", "5")
    assert (account_of(fixed)["status"], account_of(fixed)["skipped_items"]) == ("current", "0")


@pytest.mark.anyio
async def test_the_seen_flag_is_recognised_in_any_case_and_only_that_flag(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    kinds = [
        ({"\\Seen"}, "read"),
        ({"\\SEEN"}, "read"),
        ({"\\seen", "$Junk"}, "read"),
        ({"\\sEeN", "\\Answered"}, "read"),
        (set(), "unread"),
        ({"\\Answered"}, "unread"),
        ({"Seen"}, "unread"),  # a keyword of the user's own, not the system flag
    ]
    mails = []
    for index, (flags, _) in enumerate(kinds):
        one = mail(index + 1, f"s{index}@example.test", f"2026-01-05T09:{index:02d}:00Z")
        one.flags = set(flags)
        mails.append(one)
    with imap_serving(mails) as standin:
        with serving_gate(start_gate, tmp_path, standin) as gate:
            async with agent_client(gate) as client:
                listing = await call(client, LIST_TOOL, sort="received_asc")
                unread = await call(client, COUNT_TOOL, read_status="unread")

    assert [item["read_status"] for item in listing["items"]] == [want for _, want in kinds]
    assert unread["count"] == "3"


@pytest.mark.anyio
async def test_mail_beyond_any_page_of_the_window_stays_available_on_every_read(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    oldest = datetime(2025, 12, 20, tzinfo=UTC)
    mails = []
    for uid in range(1, 2101):
        when = oldest + timedelta(minutes=uid - 1)
        raw = f"From: s{uid}@example.test\r\nSubject: m{uid}\r\n\r\nbody\r\n".encode()
        mails.append(Mail(uid, raw, b'("TEXT" "PLAIN" NIL NIL NIL "7BIT" 4 1)', when))
    classifier = tmp_path / "classifier.json"
    classifier.write_text(json.dumps({"inference": {"backend": None}}))  # no text to classify
    first_minutes = {"received_from": "2025-12-20T00:00Z", "received_to": "2025-12-20T00:49Z"}
    last_minutes = {"received_from": "2025-12-21T10:50Z", "received_to": "2025-12-21T10:59Z"}
    with imap_serving(mails) as standin:
        env = {"GLOVEBOX_CLASSIFIER_CONFIG": str(classifier)}
        with serving_gate(start_gate, tmp_path, standin, extra_env=env) as gate:
            async with agent_client(gate) as client:
                reads = [
                    [
                        await call(client, COUNT_TOOL),
                        await call(client, COUNT_TOOL, **first_minutes),
                        await call(client, COUNT_TOOL, **last_minutes),
                    ]
                    for _ in range(2)
                ]

    for total, first, last in reads:
        # More than 2,000 messages in the window, the oldest ones as available as the newest.
        assert (total["count"], first["count"], last["count"]) == ("1000+", "50", "10")
        entry = account_of(total)
        assert (entry["status"], entry["skipped_items"]) == ("current", "0")


@pytest.mark.anyio
@pytest.mark.parametrize("fault", ["stall", "trickle"])
async def test_a_read_that_outlasts_its_time_limit_is_an_outage_and_the_next_one_recovers(
    start_gate: GateStarter, tmp_path: Path, fault: str
) -> None:
    with imap_serving(inbox()) as standin:
        env = {DEADLINE_VAR: "2"}
        with serving_gate(start_gate, tmp_path, standin, extra_env=env) as gate:
            async with agent_client(gate) as client:
                before = await call(client, LIST_TOOL)
                standin.fault = fault
                started = time.monotonic()
                down = await call(client, LIST_TOOL)
                counted = await call(client, COUNT_TOOL)
                elapsed = time.monotonic() - started
                standin.fault = None
                after = await call(client, LIST_TOOL)
            log = gate.log.read_text(errors="replace")

    for result in (down, counted):
        assert result["error"] == "source_unavailable"
        entry = account_of(result)
        assert (entry["status"], entry["last_sync"]) == ("stale", FIRST_SYNC)
    assert down["items"] == [] and counted["count"] == "0"
    assert elapsed < 20  # two reads at their 2-second limit, with a margin
    assert after == before
    assert_no_corpus({"down": json.dumps(down), "log": log})


# --- the hostile corpus -----------------------------------------------------------------------


@pytest.mark.anyio
async def test_no_corpus_string_reaches_any_channel_from_a_hostile_mailbox(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    faults = ("down", "bye", "auth", "select_no", "fetch_no", "fetch_drop", "garbage")
    with imap_serving(hostile_inbox()) as standin:
        with serving_gate(start_gate, tmp_path, standin) as gate:
            channels = await observe_gate(gate)
            async with agent_client(gate) as client:
                listing = await call(client, LIST_TOOL)
                for fault in faults:
                    standin.fault = fault
                    channels[f"fault {fault}"] = json.dumps(await call(client, LIST_TOOL))
                    channels[f"accounts after {fault}"] = json.dumps(
                        await call(client, ACCOUNTS_TOOL)
                    )
            channels["log"] = gate.log.read_text(errors="replace")
    files = storage_files(gate.db)

    # Every message is read, whatever its headers hold; none is lost to a corpus string.
    assert len(listing["items"]) == 2 * len(CORPUS)
    assert {item["read_status"] for item in listing["items"]} == {"unread"}
    assert account_of(listing)["status"] == "current"
    assert_no_corpus(channels)
    assert_no_corpus(files)


@pytest.mark.anyio
async def test_the_login_goes_nowhere_but_the_server(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with imap_serving(inbox()) as standin:
        with serving_gate(start_gate, tmp_path, standin) as gate:
            channels = await observe_gate(gate)
            async with agent_client(gate) as client:
                listing = await call(client, LIST_TOOL)
            channels["log"] = gate.log.read_text(errors="replace")
    everything: dict[str, str | bytes] = {**channels, **storage_files(gate.db)}

    # The stand-in refuses any other login, so a current account is the proof it was sent.
    assert account_of(listing)["status"] == "current"
    for name, content in everything.items():
        text = content if isinstance(content, str) else content.decode("utf-8", "replace")
        assert PASSWORD not in text and USER not in text, name


@pytest.mark.anyio
async def test_no_message_text_is_stored_by_the_gate(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    marker = "stored-text-marker-4711"
    mails = [mail(1, "a@example.test", "2026-01-05T09:00:00Z", subject=marker, body=marker)]
    with imap_serving(mails) as standin:
        with serving_gate(start_gate, tmp_path, standin) as gate:
            async with agent_client(gate) as client:
                await call(client, LIST_TOOL)
    for name, content in {**storage_files(gate.db), "log": gate.log.read_bytes()}.items():
        assert marker.encode() not in content, name
        assert b"a@example.test" not in content, name


# --- setting the account up -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "login", "mode", "complaint"),
    [
        ("imap://imap.mailbox.org", LOGIN, 0o600, "imaps://"),
        ("http://127.0.0.1:9101", LOGIN, 0o600, "imaps://HOST"),
        ("imaps://imap.mailbox.org/INBOX", LOGIN, 0o600, "names a server only"),
        ("imaps://user:secret@imap.mailbox.org", LOGIN, 0o600, "credentials out of the source"),
        ("imaps://imap.mailbox.org", "only-one-line\n", 0o600, "first line"),
        ("imaps://imap.mailbox.org", "user\npassword\nextra\n", 0o600, "first line"),
        ("imaps://imap.mailbox.org", LOGIN, 0o644, "accessible to group or others"),
    ],
)
def test_an_account_the_gate_could_not_use_safely_is_refused_when_it_is_added(
    tmp_path: Path, url: str, login: str, mode: int, complaint: str
) -> None:
    added = add_account(tmp_path, url, login, mode)

    assert added.returncode == 1
    assert complaint in added.stderr
    assert "secret" not in added.stderr.replace("credentials out of the source", "")
    assert operator(tmp_path / "glovebox.db", "account", "list").stdout == ""


def test_a_mailbox_org_account_needs_a_credentials_file(tmp_path: Path) -> None:
    added = operator(
        tmp_path / "glovebox.db",
        *("account", "add", ACCOUNT, "--kind", KIND, "--source", "imaps://imap.mailbox.org"),
    )

    assert added.returncode == 1 and "needs a credentials file" in added.stderr


def test_an_imap_source_belongs_to_the_mailbox_org_kind_alone(tmp_path: Path) -> None:
    added = operator(
        tmp_path / "glovebox.db",
        *("account", "add", "other", "--kind", "fixture_mail", "--source", "imaps://a.test"),
    )

    assert added.returncode == 1 and KIND in added.stderr


@pytest.mark.parametrize("url", ["imaps://imap.mailbox.org", "imap://127.0.0.1:1143"])
def test_the_documented_source_and_the_stand_in_can_be_added(tmp_path: Path, url: str) -> None:
    added = add_account(tmp_path, url)

    assert added.returncode == 0, added.stderr
    (line,) = operator(tmp_path / "glovebox.db", "account", "list").stdout.splitlines()
    assert line.split("\t")[2:4] == [KIND, url]


@pytest.mark.anyio
@pytest.mark.parametrize("change", ["mode", "format", "source"])
async def test_an_account_whose_setup_changed_is_blocked_and_nothing_is_sent(
    start_gate: GateStarter, tmp_path: Path, change: str
) -> None:
    with imap_serving(inbox()) as standin:
        with serving_gate(start_gate, tmp_path, standin) as gate:
            credentials = tmp_path / "mailbox-login"
            if change == "mode":
                credentials.chmod(0o644)
            elif change == "format":
                credentials.write_text("one line only\n")
            else:
                Access(gate.db).set_source(ACCOUNT, "imap://192.0.2.1")
            async with agent_client(gate) as client:
                result = await call(client, LIST_TOOL)

    assert result["error"] == "source_unavailable"
    assert account_of(result)["status"] == "blocked"
    assert standin.connections == 0


# --- below the seam: the connection, the attachment rule and the cap ---------------------------


class RecordingClient:
    """Stands in for IMAPClient to see how it is asked to connect; it never connects."""

    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))
        raise OSError


def test_the_login_is_sent_only_over_verified_tls_beyond_this_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(imapmail, "IMAPClient", RecordingClient)
    login = imapmail.Login(USER, Secret(PASSWORD))
    with pytest.raises(imapmail.MailboxError):
        imapmail.read_inbox(imapmail.location_of("imaps://imap.mailbox.org"), login, WINDOW_START)

    ((args, kwargs),) = RecordingClient.calls
    assert args == ("imap.mailbox.org", 993)
    context = kwargs["ssl_context"]
    assert kwargs["ssl"] is True
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


@pytest.mark.parametrize(
    "source",
    [
        "imap://imap.mailbox.org",
        "imap://10.0.0.5",
        "imap://127.evil.example",
        "imap://localhost.evil.example",
        "imaps://",
    ],
)
def test_a_source_that_would_send_the_login_in_clear_across_a_network_is_blocked(
    source: str,
) -> None:
    with pytest.raises(imapmail.MailboxBlocked):
        imapmail.location_of(source)


STRUCTURES = {
    "text": ((b"TEXT", b"PLAIN", (b"charset", b"utf-8"), None, None, b"7BIT", 6, 1), False),
    "text with a parameter named attachment": (
        (b"TEXT", b"PLAIN", (b"attachment", b"x"), None, None, b"7BIT", 6, 1),
        False,
    ),
    "text sent as an attachment": (
        (b"TEXT", b"PLAIN", None, None, None, b"7BIT", 5, 1, None, (b"ATTACHMENT", None)),
        True,
    ),
    "a file without a disposition": (
        (b"APPLICATION", b"PDF", None, None, None, b"BASE64", 5, None, None),
        True,
    ),
    "an inline image": (
        ([(b"TEXT", b"PLAIN", None, None, None, b"7BIT", 1, 1)], b"MIXED"),
        False,
    ),
    "a signature": (
        (
            [
                (b"TEXT", b"PLAIN", None, None, None, b"7BIT", 1, 1),
                (b"APPLICATION", b"PGP-SIGNATURE", None, None, None, b"7BIT", 9, None),
            ],
            b"SIGNED",
        ),
        False,
    ),
    "images of an html body": (
        (
            [
                (
                    [
                        (b"TEXT", b"HTML", None, None, None, b"7BIT", 1, 1),
                        (b"IMAGE", b"PNG", None, b"<cid>", None, b"BASE64", 9, None),
                    ],
                    b"RELATED",
                ),
                (b"TEXT", b"PLAIN", None, None, None, b"7BIT", 1, 1),
            ],
            b"ALTERNATIVE",
        ),
        False,
    ),
    "a file beside the text": (
        (
            [
                (b"TEXT", b"PLAIN", None, None, None, b"7BIT", 1, 1),
                (b"IMAGE", b"PNG", None, None, None, b"BASE64", 9, None),
            ],
            b"MIXED",
        ),
        True,
    ),
    "a forwarded message": (
        (
            [
                (b"TEXT", b"PLAIN", None, None, None, b"7BIT", 1, 1),
                (
                    b"MESSAGE",
                    b"RFC822",
                    None,
                    None,
                    None,
                    b"7BIT",
                    99,
                    (),
                    (),
                    3,
                    None,
                    (b"ATTACHMENT", None),
                ),
            ],
            b"MIXED",
        ),
        True,
    ),
}


@pytest.mark.parametrize("name", STRUCTURES)
def test_a_part_counts_as_an_attachment_only_if_it_is_meant_as_a_file(name: str) -> None:
    structure, expected = STRUCTURES[name]

    assert imapmail.has_attachment(structure) is expected


def read_standin(standin: ImapStandIn, limit: timedelta) -> imapmail.InboxRead:
    """One read of the stand-in's window, straight through the adapter."""
    location = imapmail.location_of(standin.url)
    return imapmail.read_inbox(
        location, imapmail.Login(USER, Secret(PASSWORD)), WINDOW_START, limit
    )


def test_the_whole_window_is_read_in_bounded_batches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imapmail, "BATCH_SIZE", 2)
    mails = [mail(uid, f"s{uid}@example.test", "2026-01-05T09:00:00Z") for uid in range(1, 9)]
    with imap_serving(mails) as standin:
        found = read_standin(standin, imapmail.DEFAULT_READ_DEADLINE)

    assert sorted(int(message.key.split(":")[1]) for message in found.messages) == list(range(1, 9))
    assert found.skipped == 0
    assert len(standin.fetches) == 4  # four commands of two messages, not one of eight


@pytest.fixture(params=["with the clock cutting the connection", "checks between commands only"])
def clock(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """The time limit with and without the watchdog that shuts the connection at the limit:
    the checks the adapter makes itself must hold without it."""
    if request.param.startswith("checks"):
        monkeypatch.setattr(
            imapmail.Budget, "enforce", lambda self, client: threading.Timer(0, int)
        )
    return str(request.param)


def test_a_read_that_retries_message_by_message_still_keeps_to_its_time_limit(clock: str) -> None:
    mails = [mail(uid, f"s{uid}@example.test", "2026-01-05T09:00:00Z") for uid in range(1, 41)]
    mails[2] = dataclasses.replace(mails[2], structure=b"(((((")  # the batch cannot be parsed
    with imap_serving(mails) as standin:
        standin.fetch_delay = 0.1
        with pytest.raises(imapmail.MailboxError):
            read_standin(standin, timedelta(seconds=1))

    # Forty single fetches would take four seconds: the read stopped at its limit.
    assert len(standin.fetches) < 20


def test_a_read_whose_last_command_outlasts_the_time_limit_is_an_error_not_a_success(
    clock: str,
) -> None:
    with imap_serving([mail(1, "a@example.test", "2026-01-05T09:00:00Z")]) as standin:
        standin.fetch_delay = 1.5
        with pytest.raises(imapmail.MailboxError):
            read_standin(standin, timedelta(seconds=1))


def test_a_logout_that_stalls_is_cut_off_at_the_time_limit() -> None:
    with imap_serving([mail(1, "a@example.test", "2026-01-05T09:00:00Z")]) as standin:
        standin.fault = "logout_stall"
        started = time.monotonic()
        with pytest.raises(imapmail.MailboxError):
            read_standin(standin, timedelta(seconds=1))

    assert time.monotonic() - started < imapmail.CONNECT_TIMEOUT_SECONDS - 1  # not the socket's


@pytest.mark.parametrize("value", ["0", "-1", "soon", "1.5", "3601", "99999999999999999999"])
def test_a_bad_read_deadline_stops_the_service_from_starting(tmp_path: Path, value: str) -> None:
    started = subprocess.run(
        [sys.executable, "-m", "glovebox"],
        env={
            **clean_environment(tmp_path / "glovebox.db"),
            "GLOVEBOX_PORT": str(free_port()),
            DEADLINE_VAR: value,
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert started.returncode == 1
    assert DEADLINE_VAR in started.stderr
    assert value not in started.stderr.replace("1 to 3600", "")  # the message's own range
