"""Operator setup and agent tokens, at the two seams that matter (ADR 0004, ADR 0006).

The operator's command line is driven as a real process against the database file, and
agents are real MCP clients over HTTP. A few tests reach below the seam on purpose, each
marked: the stored files (a token value and a credential must not be in them) and the
service's log.
"""

import hashlib
import os
import sqlite3
import stat
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx2
import pytest

from tests.client import (
    ACCOUNTS_TOOL,
    ALL_TOOLS,
    COUNT_TOOL,
    GROUP_TOOL,
    LIST_TOOL,
    agent_client,
)
from tests.conftest import (
    DEFAULT_ACCOUNT,
    GateStarter,
    RunningGate,
    clean_environment,
    operator,
    run_gate,
)
from tests.hostile import assert_no_corpus, hostile_mail_fixture, observe_gate
from tests.test_gate import small_fixture, storage_files, write_fixture
from tests.test_queries import call, mail, mailbox

ACCEPT = {"Accept": "application/json, text/event-stream"}
ABSENT_HANDLE = "abcdefghijklmnopqrstuvwx"  # shaped like a handle, issued to no one
SECRET = "GBX-CREDENTIAL-SECRET-4f9c1e7a-do-not-leak"


def numbered(prefix: str, count: int) -> dict[str, object]:
    """A mailbox of `count` items, from seven senders, all inside the window."""
    return mailbox(
        [
            mail(
                f"{prefix}{i}",
                f"{prefix}sender{i % 7}",
                f"2026-01-05T{i // 60:02d}:{i % 60:02d}:00Z",
                read=bool(i % 2),
            )
            for i in range(count)
        ]
    )


def bob_fixture() -> dict[str, object]:
    return mailbox(
        [
            mail("b1", "zed", "2026-01-07T09:00:00Z", read=False, thread="bt1"),
            mail("b2", "zed", "2026-01-07T10:00:00Z", read=True, thread="bt1"),
        ]
    )


def token_id(db: Path, label: str) -> str:
    """The id of the token with this label, from `token list`."""
    for line in operator(db, "token", "list").stdout.splitlines():
        fields = line.split("\t")
        if fields[5] == label:
            return fields[0]
    raise AssertionError("no such token")


def two_accounts(tmp_path: Path) -> dict[str, Path]:
    return {
        "alice": write_fixture(tmp_path / "alice.json", small_fixture()),
        "bob": write_fixture(tmp_path / "bob.json", bob_fixture()),
    }


SCOPES = {"a": ["alice"], "b": ["bob"], "both": ["alice", "bob"]}


async def account_handles(gate: RunningGate, label: str) -> list[str]:
    async with agent_client(gate, token=gate.tokens[label]) as client:
        listed = await call(client, ACCOUNTS_TOOL)
    return [entry["account_handle"] for entry in listed["accounts"]]


# --- rejecting tokens -------------------------------------------------------------------


def test_absent_wrong_and_revoked_tokens_are_rejected_on_every_request(
    start_gate: GateStarter,
) -> None:
    call_body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    with start_gate() as gate, httpx2.Client(timeout=30) as http:
        db = gate.db
        for headers in (
            {},
            {"Authorization": "Bearer"},
            {"Authorization": "Bearer gbx_not-a-token-that-was-ever-issued-0000"},
            {"Authorization": f"Bearer {gate.token}x"},
            {"Authorization": f"Bearer {gate.token[:-1]}"},
            {"Authorization": f"Bearer {gate.token}", "X-Extra": "ok"},
        ):
            posted = http.post(gate.url, json=call_body, headers={**ACCEPT, **headers})
            should_pass = headers.get("Authorization") == f"Bearer {gate.token}"
            assert (posted.status_code != 401) == should_pass, headers

        revoked = operator(db, "token", "revoke", token_id(db, "default"))
        assert revoked.returncode == 0, revoked.stderr
        again = http.post(
            gate.url,
            json=call_body,
            headers={**ACCEPT, "Authorization": f"Bearer {gate.token}"},
        )
        assert again.status_code == 401
        assert again.text == '{"error": "unauthorized"}'


@pytest.mark.anyio
async def test_a_token_revoked_during_a_session_is_refused_on_its_next_request(
    start_gate: GateStarter,
) -> None:
    with start_gate(mail_fixture=None) as gate:
        async with agent_client(gate) as client:
            first = await client.call_tool(ACCOUNTS_TOOL)
            assert first.is_error is False
            assert (
                operator(gate.db, "token", "revoke", token_id(gate.db, "default")).returncode == 0
            )
            with pytest.raises(Exception):  # noqa: B017 - the client's error type is not the contract
                await client.call_tool(ACCOUNTS_TOOL)
        # Other tokens are not affected.
    with start_gate(tokens={"default": [DEFAULT_ACCOUNT], "other": [DEFAULT_ACCOUNT]}) as gate:
        assert gate.tokens["other"] != gate.tokens["default"]
        async with agent_client(gate, token=gate.tokens["other"]) as client:
            assert (await client.call_tool(ACCOUNTS_TOOL)).is_error is False


@pytest.mark.anyio
async def test_the_static_token_setting_no_longer_grants_access(tmp_path: Path) -> None:
    static = "static-agent-token-0123456789abcdef"
    with run_gate(
        tmp_path / "glovebox.db",
        accounts={DEFAULT_ACCOUNT: write_fixture(tmp_path / "m.json", small_fixture())},
        extra_env={"GLOVEBOX_AGENT_TOKEN": static},
    ) as gate:
        with pytest.raises(Exception):  # noqa: B017 - the client's error type is not the contract
            async with agent_client(gate, token=static) as client:
                await client.list_tools()
        async with agent_client(gate) as client:
            assert (await client.call_tool(ACCOUNTS_TOOL)).is_error is False


def test_a_gate_with_nothing_set_up_starts_and_refuses_everyone(tmp_path: Path) -> None:
    with run_gate(tmp_path / "empty.db", accounts={}, tokens={}) as gate:
        refused = httpx2.post(
            gate.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={**ACCEPT, "Authorization": "Bearer gbx_nothing-was-ever-issued-0000000000"},
        )
    assert refused.status_code == 401


# --- account scopes ---------------------------------------------------------------------


@pytest.mark.anyio
async def test_each_token_lists_counts_groups_and_filters_only_its_own_accounts(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with start_gate(accounts=two_accounts(tmp_path), tokens=SCOPES) as gate:
        handles = {label: await account_handles(gate, label) for label in SCOPES}
        assert len(handles["a"]) == len(handles["b"]) == 1
        assert handles["both"] == sorted(handles["both"], key=lambda h: h != handles["a"][0])
        alice, bob = handles["a"][0], handles["b"][0]
        assert alice != bob and set(handles["both"]) == {alice, bob}

        async def items(label: str, **arguments: object) -> list[dict[str, Any]]:
            async with agent_client(gate, token=gate.tokens[label]) as client:
                listed = await call(client, LIST_TOOL, **arguments)
            assert listed["error"] == "none"
            result: list[dict[str, Any]] = listed["items"]
            return result

        async def count(label: str, **arguments: object) -> str:
            async with agent_client(gate, token=gate.tokens[label]) as client:
                counted = await call(client, COUNT_TOOL, **arguments)
            result: str = counted["count"]
            return result

        assert {item["account_handle"] for item in await items("a")} == {alice}
        assert {item["account_handle"] for item in await items("b")} == {bob}
        assert len(await items("a")) == 3 and len(await items("b")) == 2
        assert len(await items("both")) == 5
        assert [await count(label) for label in SCOPES] == ["3", "2", "5"]
        # A filter narrows within the scope; the account handle filter picks one account.
        assert await count("both", account_handle=bob) == "2"
        assert await count("both", account_handle=alice, read_status="unread") == "1"
        assert await count("a", read_status="read") == "1"
        assert {i["account_handle"] for i in await items("both", account_handle=alice)} == {alice}

        async with agent_client(gate, token=gate.tokens["a"]) as client:
            by_sender = await call(client, GROUP_TOOL, group_by="sender_handle")
            by_kind = await call(client, GROUP_TOOL, group_by="source_kind")
        assert sorted(group["count"] for group in by_sender["groups"]) == ["1", "2"]
        assert by_kind["groups"] == [{"key": "fixture_mail", "count": "3"}]
        async with agent_client(gate, token=gate.tokens["both"]) as client:
            both_kind = await call(client, GROUP_TOOL, group_by="source_kind")
        assert both_kind["groups"] == [{"key": "fixture_mail", "count": "5"}]


@pytest.mark.anyio
async def test_paging_stays_inside_the_token_scope(start_gate: GateStarter, tmp_path: Path) -> None:
    accounts = {
        "alice": write_fixture(tmp_path / "alice.json", numbered("a", 120)),
        "bob": write_fixture(tmp_path / "bob.json", numbered("b", 70)),
    }
    with start_gate(accounts=accounts, tokens=SCOPES) as gate:
        alice, bob = (await account_handles(gate, "a"))[0], (await account_handles(gate, "b"))[0]
        seen: dict[str, list[str]] = {}
        for label, expected in (("a", 120), ("b", 70), ("both", 190)):
            handles: list[str] = []
            async with agent_client(gate, token=gate.tokens[label]) as client:
                page = await call(client, LIST_TOOL)
                while True:
                    assert page["error"] == "none" and len(page["items"]) <= 50
                    handles += [item["item_handle"] for item in page["items"]]
                    owners = {item["account_handle"] for item in page["items"]}
                    assert owners <= (
                        {alice} if label == "a" else {bob} if label == "b" else {alice, bob}
                    )
                    if page["next_cursor"] is None:
                        break
                    page = await call(client, LIST_TOOL, cursor=page["next_cursor"])
            assert len(handles) == len(set(handles)) == expected
            seen[label] = handles
        assert set(seen["a"]) | set(seen["b"]) == set(seen["both"])
        assert set(seen["a"]).isdisjoint(seen["b"])


# --- indistinguishable absence ----------------------------------------------------------


@pytest.mark.anyio
async def test_handles_and_cursors_of_another_scope_are_answered_like_absent_ones(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    accounts = {
        "alice": write_fixture(tmp_path / "alice.json", numbered("a", 60)),
        "bob": write_fixture(tmp_path / "bob.json", numbered("b", 60)),
    }
    with start_gate(accounts=accounts, tokens=SCOPES) as gate:
        async with agent_client(gate, token=gate.tokens["b"]) as bobs:
            bob_items = await call(bobs, LIST_TOOL, limit=1)
            bob_groups = await call(bobs, GROUP_TOOL, group_by="sender_handle", limit=1)
        foreign = bob_items["items"][0]
        foreign_filters = {
            "account_handle": foreign["account_handle"],
            "sender_handle": foreign["sender_handle"],
            "thread_handle": foreign["thread_handle"],
        }

        async with agent_client(gate, token=gate.tokens["a"]) as alices:
            for name, handle in foreign_filters.items():
                for tool, extra in (
                    (LIST_TOOL, {}),
                    (COUNT_TOOL, {}),
                    (GROUP_TOOL, {"group_by": "sender_handle"}),
                ):
                    out_of_scope = await alices.call_tool(tool, {name: handle, **extra})
                    absent = await alices.call_tool(tool, {name: ABSENT_HANDLE, **extra})
                    assert out_of_scope.model_dump_json() == absent.model_dump_json(), (tool, name)
                    assert out_of_scope.is_error is False
                    structured = out_of_scope.structured_content
                    assert structured is not None
                    assert structured.get("items", structured.get("groups", [])) == []
                    assert structured.get("count", "0") == "0"

            for tool, arguments, cursor in (
                (LIST_TOOL, {}, bob_items["next_cursor"]),
                (GROUP_TOOL, {"group_by": "sender_handle"}, bob_groups["next_cursor"]),
            ):
                assert cursor is not None
                foreign_cursor = await alices.call_tool(tool, {**arguments, "cursor": cursor})
                forged_cursor = await alices.call_tool(tool, {**arguments, "cursor": "A" * 27})
                assert foreign_cursor.model_dump_json() == forged_cursor.model_dump_json()
                assert foreign_cursor.structured_content is not None
                assert foreign_cursor.structured_content["error"] == "invalid_cursor"

        # A token that covers both accounts still cannot use another token's cursor.
        async with agent_client(gate, token=gate.tokens["both"]) as everyone:
            refused = await call(everyone, LIST_TOOL, cursor=bob_items["next_cursor"])
        assert refused["error"] == "invalid_cursor"


# --- read scopes ------------------------------------------------------------------------


@pytest.mark.anyio
async def test_a_token_limited_to_some_operations_sees_only_those_tools(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    fixture = write_fixture(tmp_path / "alice.json", small_fixture())
    with start_gate(mail_fixture=fixture) as gate:
        issued = operator(
            gate.db,
            "token",
            "issue",
            "--account",
            DEFAULT_ACCOUNT,
            "--operation",
            "count_items",
            "--label",
            "counter",
        )
        assert issued.returncode == 0, issued.stderr
        async with agent_client(gate, token=issued.stdout.strip()) as client:
            tools = [tool.name for tool in (await client.list_tools()).tools]
            counted = await call(client, COUNT_TOOL)
            hidden = await client.call_tool(LIST_TOOL, {})
            accounts = await client.call_tool(ACCOUNTS_TOOL, {})
            nonexistent = await client.call_tool("no_such_tool", {})

    assert tools == [COUNT_TOOL]
    assert (counted["error"], counted["count"]) == ("none", "3")
    assert [account["status"] for account in counted["accounts"]] == ["current"]
    for refused in (hidden, accounts):
        assert refused.is_error is True
        assert refused.model_dump_json() == nonexistent.model_dump_json()
        assert refused.structured_content == {"error": "unknown_tool"}


@pytest.mark.anyio
async def test_the_accounts_tool_gives_only_handle_source_kind_and_sync_status(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with start_gate(accounts=two_accounts(tmp_path), tokens=SCOPES) as gate:
        async with agent_client(gate, token=gate.tokens["both"]) as client:
            result = await client.call_tool(ACCOUNTS_TOOL, {})
            rejected = await client.call_tool(ACCOUNTS_TOOL, {"account": "alice"})

    structured = result.structured_content
    assert structured is not None and result.content == []
    assert set(structured) == {"error", "accounts"}
    assert len(structured["accounts"]) == 2
    for entry in structured["accounts"]:
        assert set(entry) == {
            "account_handle",
            "source_kind",
            "read_only_enforcement",
            "status",
            "last_sync",
            "skipped_items",
        }
        assert entry["source_kind"] == "fixture_mail"
        # Listing reads no source: nothing has synced yet, and the entry says so.
        assert (entry["status"], entry["last_sync"], entry["skipped_items"]) == (
            "stale",
            None,
            "0",
        )
    # The operator's names for the accounts are not part of anything an agent sees.
    assert "alice" not in result.model_dump_json() and "bob" not in result.model_dump_json()
    assert rejected.structured_content == {"error": "invalid_arguments", "accounts": []}


@pytest.mark.anyio
async def test_nothing_of_the_operators_setup_is_reachable_through_mcp(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with start_gate(accounts=two_accounts(tmp_path), tokens=SCOPES) as gate:
        before = (
            operator(gate.db, "account", "list").stdout,
            operator(gate.db, "token", "list").stdout,
        )
        async with agent_client(gate, token=gate.tokens["both"]) as client:
            names = [tool.name for tool in (await client.list_tools()).tools]
            for tool in ("add_account", "remove_account", "issue_token", "revoke_token"):
                result = await client.call_tool(tool, {"name": "mallory", "account": "alice"})
                assert result.structured_content == {"error": "unknown_tool"}
        with httpx2.Client(timeout=30) as http:
            for method, path in (("POST", "/accounts"), ("DELETE", "/tokens"), ("GET", "/admin")):
                response = http.request(
                    method,
                    gate.url.replace("/mcp", path),
                    headers={"Authorization": f"Bearer {gate.tokens['both']}"},
                )
                assert response.status_code in (404, 405)
        after = (
            operator(gate.db, "account", "list").stdout,
            operator(gate.db, "token", "list").stdout,
        )

    assert names == ALL_TOOLS
    assert before == after


# --- the operator's command line --------------------------------------------------------


def test_accounts_can_be_added_listed_and_removed_and_bad_input_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "glovebox.db"
    source = write_fixture(tmp_path / "mail.json", small_fixture())

    added = operator(
        db, "account", "add", "work", "--kind", "fixture_mail", "--source", str(source)
    )
    assert added.returncode == 0, added.stderr
    listed = operator(db, "account", "list")
    fields = listed.stdout.strip().split("\t")
    assert fields[0] == "work" and fields[2] == "fixture_mail" and fields[3] == str(source)
    assert len(fields[1]) == 24
    assert fields[1] in added.stdout

    refused = [
        ("account", "add", "work", "--kind", "fixture_mail", "--source", str(source)),
        ("account", "add", "Bad Name", "--kind", "fixture_mail", "--source", str(source)),
        ("account", "add", "x", "--kind", "gmail", "--source", str(source)),
        ("account", "add", "x", "--kind", "fixture_mail", "--source", "http://u:pw@h.test/m"),
        ("account", "add", "x", "--kind", "fixture_mail", "--source", "http://h.test/m?k=v"),
        ("account", "remove", "nobody"),
        ("token", "issue", "--account", "nobody"),
        ("token", "issue", "--account", "work", "--operation", "send_email"),
        ("token", "issue", "--account", "work", "--label", "bad;label"),
        ("token", "issue"),
        ("token", "revoke", "nosuchid"),
    ]
    for arguments in refused:
        result = operator(db, *arguments)
        assert result.returncode != 0, arguments
        assert result.stdout == "", arguments
    assert operator(db, "account", "list").stdout == listed.stdout

    removed = operator(db, "account", "remove", "work")
    assert removed.returncode == 0
    assert operator(db, "account", "list").stdout == ""


def test_the_database_is_private_to_the_operators_user(tmp_path: Path) -> None:
    db = tmp_path / "glovebox.db"
    previous = os.umask(0)
    try:
        assert operator(db, "account", "list").returncode == 0
    finally:
        os.umask(previous)

    assert stat.S_IMODE(db.stat().st_mode) & 0o077 == 0


@pytest.mark.anyio
async def test_a_removed_account_is_gone_with_its_items_and_its_place_in_every_scope(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    with start_gate(accounts=two_accounts(tmp_path), tokens=SCOPES) as gate:
        async with agent_client(gate, token=gate.tokens["both"]) as client:
            assert (await call(client, COUNT_TOOL))["count"] == "5"
        removed = operator(gate.db, "account", "remove", "bob")
        assert removed.returncode == 0, removed.stderr
        async with agent_client(gate, token=gate.tokens["both"]) as client:
            assert (await call(client, COUNT_TOOL))["count"] == "3"
            assert len((await call(client, ACCOUNTS_TOOL))["accounts"]) == 1
        async with agent_client(gate, token=gate.tokens["b"]) as client:
            assert (await call(client, COUNT_TOOL))["count"] == "0"
            assert (await call(client, ACCOUNTS_TOOL))["accounts"] == []
            assert (await call(client, LIST_TOOL))["items"] == []
        # Below the seam on purpose: nothing of the account is left stored.
        with sqlite3.connect(gate.db) as database:
            owners = {row[0] for row in database.execute("select account_handle from items")}
            assert len(owners) == 1


# --- token values -----------------------------------------------------------------------


def test_a_token_value_is_shown_once_and_the_database_holds_no_usable_copy(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    source = write_fixture(tmp_path / "mail.json", small_fixture())
    db = tmp_path / "glovebox.db"
    assert (
        operator(
            db, "account", "add", "work", "--kind", "fixture_mail", "--source", str(source)
        ).returncode
        == 0
    )

    issued = operator(db, "token", "issue", "--account", "work", "--label", "agent-one")

    assert issued.returncode == 0, issued.stderr
    value = issued.stdout.strip()
    assert issued.stdout.count(value) == 1 and issued.stdout == value + "\n"
    assert value.startswith("gbx_") and len(value) >= 40
    assert value not in issued.stderr
    # Not shown again: not by `token list` and not by any other command.
    for arguments in (("token", "list"), ("account", "list")):
        listing = operator(db, *arguments)
        assert value not in listing.stdout + listing.stderr
    record = operator(db, "token", "list").stdout.strip().split("\t")
    assert record[1] == "active" and record[3] == "work" and record[5] == "agent-one"
    assert record[4] == ",".join(ALL_TOOLS)
    # Below the seam on purpose: the stored files hold the verifier, not the value, and the
    # verifier is not itself a token.
    files = storage_files(db)
    digest = hashlib.sha256(value.encode()).hexdigest()
    assert all(value.encode() not in content for content in files.values())
    assert any(digest.encode() in content for content in files.values())
    with start_gate(accounts={"work": source}, tokens={}) as gate:
        for presented in (digest, f"gbx_{digest}", digest[:43]):
            refused = httpx2.post(
                gate.url,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={**ACCEPT, "Authorization": f"Bearer {presented}"},
            )
            assert refused.status_code == 401
        accepted = httpx2.post(
            gate.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={**ACCEPT, "Authorization": f"Bearer {value}"},
        )
        assert accepted.status_code != 401


def test_revoking_is_repeatable_and_leaves_other_tokens_alone(tmp_path: Path) -> None:
    db = tmp_path / "glovebox.db"
    source = write_fixture(tmp_path / "mail.json", small_fixture())
    operator(db, "account", "add", "work", "--kind", "fixture_mail", "--source", str(source))
    for label in ("one", "two"):
        assert operator(db, "token", "issue", "--account", "work", "--label", label).returncode == 0

    first = operator(db, "token", "revoke", token_id(db, "one"))
    second = operator(db, "token", "revoke", token_id(db, "one"))

    assert first.returncode == second.returncode == 0
    assert "already revoked" in second.stdout
    states = {
        line.split("\t")[5]: line.split("\t")[1].split()[0]
        for line in operator(db, "token", "list").stdout.splitlines()
    }
    assert states == {"one": "revoked", "two": "active"}


# --- credentials ------------------------------------------------------------------------


def credentials_file(directory: Path, mode: int) -> Path:
    path = directory / "credentials"
    path.unlink(missing_ok=True)  # a file left read-only by the last call cannot be rewritten
    path.write_text(SECRET)
    path.chmod(mode)
    return path


def test_an_account_whose_credentials_file_is_open_to_group_or_others_is_refused(
    tmp_path: Path,
) -> None:
    db = tmp_path / "glovebox.db"
    source = write_fixture(tmp_path / "mail.json", small_fixture())
    base = ("account", "add", "work", "--kind", "fixture_mail", "--source", str(source))
    for mode in (0o644, 0o640, 0o604, 0o660, 0o666, 0o700 | 0o010):
        refused = operator(db, *base, "--credentials", str(credentials_file(tmp_path, mode)))
        assert refused.returncode != 0, oct(mode)
        assert "accessible to group or others" in refused.stderr
        assert SECRET not in refused.stdout + refused.stderr
    assert operator(db, "account", "list").stdout == ""
    for refused_path in (tmp_path / "missing", tmp_path):
        result = operator(db, *base, "--credentials", str(refused_path))
        assert result.returncode != 0
    assert operator(db, "account", "list").stdout == ""

    # Closed to group and others is enough, whether or not the owner can write.
    for mode in (0o400, 0o600):
        added = operator(db, *base, "--credentials", str(credentials_file(tmp_path, mode)))
        assert added.returncode == 0, (oct(mode), added.stderr)
        assert operator(db, "account", "remove", "work").returncode == 0
    added = operator(db, *base, "--credentials", str(credentials_file(tmp_path, 0o600)))
    assert added.returncode == 0, added.stderr
    status = operator(db, "account", "list").stdout.strip().split("\t")
    assert status[4] == str(tmp_path / "credentials") and status[5] == "ok"
    assert SECRET not in operator(db, "account", "list").stdout


@pytest.mark.anyio
async def test_the_gate_refuses_to_serve_an_account_whose_credentials_file_became_open(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    db = tmp_path / "glovebox.db"
    source = write_fixture(tmp_path / "mail.json", small_fixture())
    credentials = credentials_file(tmp_path, 0o600)
    added = operator(
        db,
        "account",
        "add",
        DEFAULT_ACCOUNT,
        "--kind",
        "fixture_mail",
        "--source",
        str(source),
        "--credentials",
        str(credentials),
    )
    assert added.returncode == 0, added.stderr
    with start_gate(accounts={DEFAULT_ACCOUNT: source}) as gate:
        async with agent_client(gate) as client:
            assert (await call(client, COUNT_TOOL))["count"] == "3"

            credentials.chmod(0o640)
            refused = {
                tool: await client.call_tool(tool, OK_ARGUMENTS[tool])
                for tool in (LIST_TOOL, COUNT_TOOL, GROUP_TOOL)
            }
            still_listed = await call(client, ACCOUNTS_TOOL)

            credentials.chmod(0o600)
            restored = await call(client, COUNT_TOOL)

    for tool, result in refused.items():
        assert result.is_error is True and result.content == []
        assert result.structured_content is not None
        assert result.structured_content["error"] == "source_unavailable", tool
        assert SECRET not in result.model_dump_json()
    assert len(still_listed["accounts"]) == 1
    assert restored["count"] == "3"
    log = gate.log.read_text()
    assert "accessible to group or others" in log and SECRET not in log


OK_ARGUMENTS: dict[str, dict[str, object]] = {
    LIST_TOOL: {},
    COUNT_TOOL: {},
    GROUP_TOOL: {"group_by": "sender_handle"},
}


@pytest.mark.anyio
async def test_no_token_credential_or_verifier_reaches_a_response_a_log_or_the_stored_files(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    db = tmp_path / "glovebox.db"
    fixture = write_fixture(tmp_path / "hostile.json", hostile_mail_fixture())
    credentials = credentials_file(tmp_path, 0o600)
    outputs = [
        operator(
            db,
            "account",
            "add",
            DEFAULT_ACCOUNT,
            "--kind",
            "fixture_mail",
            "--source",
            str(fixture),
            "--credentials",
            str(credentials),
        )
    ]
    with start_gate(accounts={DEFAULT_ACCOUNT: fixture}) as gate:
        channels = await observe_gate(gate)
        async with agent_client(gate) as client:
            channels["listing"] = (await client.call_tool(LIST_TOOL, {})).model_dump_json()
        with httpx2.Client(timeout=30) as http:
            wrong = http.post(
                gate.url,
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={**ACCEPT, "Authorization": f"Bearer {gate.token}wrong"},
            )
            channels["wrong-token"] = f"{wrong.headers} {wrong.text}"
        credentials.chmod(0o666)
        async with agent_client(gate) as client:
            channels["unavailable"] = (await client.call_tool(LIST_TOOL, {})).model_dump_json()
        outputs += [operator(db, "account", "list"), operator(db, "token", "list")]

    everything: dict[str, str | bytes] = {
        **channels,
        **storage_files(db),
        "log": gate.log.read_bytes(),
    }
    for number, output in enumerate(outputs):
        everything[f"cli-{number}"] = output.stdout + output.stderr
    digest = hashlib.sha256(gate.token.encode()).hexdigest()
    for name, content in everything.items():
        data = content if isinstance(content, bytes) else content.encode()
        assert SECRET.encode() not in data, name
        assert gate.token.encode() not in data, name
        # The verifier belongs in the database and nowhere else.
        if not name.startswith("glovebox.db"):
            assert digest.encode() not in data, name
    assert_no_corpus({name: value for name, value in everything.items() if name != "log"})


# --- the fixture stacks -----------------------------------------------------------------


FIXTURE_TOKEN = "fixture-stack-token-0123456789abcdefghij"


def fixture_env(source: Path, **extra: str) -> dict[str, str]:
    return {
        "GLOVEBOX_FIXTURE_TOKEN": FIXTURE_TOKEN,
        "GLOVEBOX_MAIL_FIXTURE": str(source),
        **extra,
    }


@pytest.mark.anyio
async def test_the_fixture_bootstrap_makes_the_stack_token_work_and_survives_restarts(
    tmp_path: Path,
) -> None:
    db = tmp_path / "stack.db"
    first = write_fixture(tmp_path / "first.json", small_fixture())
    second = write_fixture(tmp_path / "second.json", bob_fixture())

    def start(source: Path) -> Any:
        return run_gate(
            db,
            accounts={},
            tokens={},
            extra_env=fixture_env(source),
            command=("serve", "--bootstrap-fixture"),
        )

    with start(first) as gate:
        async with agent_client(gate, token=FIXTURE_TOKEN) as client:
            accounts = await call(client, ACCOUNTS_TOOL)
            counted = await call(client, COUNT_TOOL)
        assert counted["count"] == "3" and len(accounts["accounts"]) == 1
        handle = accounts["accounts"][0]["account_handle"]
        # Below the seam on purpose: only the verifier of the stack token is stored.
        assert all(FIXTURE_TOKEN.encode() not in data for data in storage_files(db).values())
    with start(second) as gate:
        async with agent_client(gate, token=FIXTURE_TOKEN) as client:
            counted = await call(client, COUNT_TOOL)
            again = await call(client, ACCOUNTS_TOOL)
        # The same token and account, now pointed at the new source.
        assert counted["count"] == "2"
        assert again["accounts"][0]["account_handle"] == handle
        assert len(operator(db, "token", "list").stdout.splitlines()) == 1

    revoked = operator(db, "token", "revoke", token_id(db, "fixture"))
    assert revoked.returncode == 0
    with start(second) as gate:
        # Starting again does not bring a revoked token back.
        with pytest.raises(Exception):  # noqa: B017 - the client's error type is not the contract
            async with agent_client(gate, token=FIXTURE_TOKEN) as client:
                await client.list_tools()


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"GLOVEBOX_FIXTURE_TOKEN": "short"},
        {"GLOVEBOX_FIXTURE_TOKEN": FIXTURE_TOKEN},
        {
            "GLOVEBOX_FIXTURE_TOKEN": FIXTURE_TOKEN,
            "GLOVEBOX_MAIL_FIXTURE": "/a.json",
            "GLOVEBOX_STANDIN_MAILBOX_MAIL": "http://127.0.0.1:1",
        },
        {"GLOVEBOX_FIXTURE_TOKEN": " " * 40, "GLOVEBOX_MAIL_FIXTURE": "/a.json"},
    ],
)
def test_the_fixture_bootstrap_refuses_an_incomplete_or_unusable_setup(
    tmp_path: Path, env: dict[str, str]
) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "glovebox", "serve", "--bootstrap-fixture"],
        env=clean_environment(tmp_path / "glovebox.db", **env),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode != 0
    assert "short" not in result.stderr and FIXTURE_TOKEN not in result.stdout + result.stderr


def test_the_gate_says_at_start_which_accounts_it_will_not_serve(
    start_gate: GateStarter, tmp_path: Path
) -> None:
    db = tmp_path / "glovebox.db"
    source = write_fixture(tmp_path / "mail.json", small_fixture())
    credentials = credentials_file(tmp_path, 0o600)
    base = ("account", "add", DEFAULT_ACCOUNT, "--kind", "fixture_mail", "--source", str(source))
    assert operator(db, *base, "--credentials", str(credentials)).returncode == 0
    credentials.chmod(0o644)

    with start_gate(accounts={DEFAULT_ACCOUNT: source}) as gate:
        pass

    log = gate.log.read_text()
    assert f"account {DEFAULT_ACCOUNT} is not served" in log
    assert "accessible to group or others" in log and SECRET not in log


def test_revoking_does_not_repeat_what_was_typed_when_it_is_no_id(tmp_path: Path) -> None:
    db = tmp_path / "glovebox.db"
    source = write_fixture(tmp_path / "mail.json", small_fixture())
    operator(db, "account", "add", "work", "--kind", "fixture_mail", "--source", str(source))
    value = operator(db, "token", "issue", "--account", "work").stdout.strip()

    pasted = operator(db, "token", "revoke", value)

    assert pasted.returncode != 0
    assert value not in pasted.stdout + pasted.stderr
    # The token is still active: nothing was revoked by mistake.
    assert "active" in operator(db, "token", "list").stdout


@pytest.mark.anyio
async def test_the_fixture_token_covers_the_fixture_account_again_after_it_was_removed(
    tmp_path: Path,
) -> None:
    db = tmp_path / "stack.db"
    source = write_fixture(tmp_path / "mail.json", small_fixture())

    def start() -> Any:
        return run_gate(
            db,
            accounts={},
            tokens={},
            extra_env=fixture_env(source),
            command=("serve", "--bootstrap-fixture"),
        )

    with start():
        pass
    assert operator(db, "account", "remove", "fixture").returncode == 0
    with start() as gate:
        async with agent_client(gate, token=FIXTURE_TOKEN) as client:
            assert (await call(client, COUNT_TOOL))["count"] == "3"


def test_the_commands_of_the_operator_do_not_need_the_service_to_run(tmp_path: Path) -> None:
    """`account` and `token` work on the file alone; no server is listening anywhere."""
    db = tmp_path / "glovebox.db"
    source = write_fixture(tmp_path / "mail.json", small_fixture())
    steps: Sequence[tuple[str, ...]] = (
        ("account", "add", "work", "--kind", "fixture_mail", "--source", str(source)),
        ("token", "issue", "--account", "work"),
        ("token", "list"),
    )
    assert all(operator(db, *step).returncode == 0 for step in steps)
