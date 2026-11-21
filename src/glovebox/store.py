"""SQLite persistence: permitted fields, handle mappings and sync state, nothing else.

Raw text is fetched, reduced to permitted fields and discarded (ADR 0004). The
handle registry therefore does not keep upstream IDs, sender addresses or thread
keys: it keeps a keyed digest of each, with a key that exists only in this
database, which is enough to give the same upstream entity the same handle again.
Handles themselves come from randomness and have no relation to what they stand for.
"""

import hashlib
import hmac
import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from glovebox.formats import (
    COUNT_CAP,
    COUNT_CAPPED,
    HANDLE_ALPHABET,
    HANDLE_LENGTH,
    MINUTE_FORMAT,
    WINDOW_DAYS,
)
from glovebox.query import Filters, GroupBy, SortOrder
from glovebox.sources import SourceKind, SourceMailbox

BUSY_TIMEOUT_SECONDS = 30
MINT_ATTEMPTS = 16

# The fixed sort orders, each ending in the item handle so that a page boundary is stable.
SORT_ORDERS: dict[SortOrder, str] = {
    "received_desc": "received desc, item_handle",
    "received_asc": "received asc, item_handle",
    "unread_first": (
        "case read_status when 'unread' then 0 when 'unknown' then 1 else 2 end,"
        " received desc, item_handle"
    ),
    "attachments_first": "has_attachments desc, received desc, item_handle",
}
GROUP_COLUMNS: dict[GroupBy, str] = {
    "sender_handle": "sender_handle",
    "thread_handle": "thread_handle",
    "read_status": "read_status",
    "source_kind": "source_kind",
}

SCHEMA = """
create table if not exists meta (
    key text primary key,
    value text not null
);
create table if not exists handles (
    handle text primary key,
    kind text not null,
    digest text not null,
    unique (kind, digest)
);
create table if not exists items (
    item_handle text primary key,
    account_handle text not null,
    source_kind text not null,
    sender_handle text not null,
    thread_handle text not null,
    received text not null,
    read_status text not null,
    has_attachments integer not null
);
create index if not exists items_by_received on items (received desc, item_handle);
create table if not exists sync_state (
    account_handle text primary key,
    source_kind text not null,
    status text not null,
    last_sync text not null
);
"""


def minute(moment: datetime) -> str:
    return moment.strftime(MINUTE_FORMAT)


def new_handle() -> str:
    return "".join(secrets.choice(HANDLE_ALPHABET) for _ in range(HANDLE_LENGTH))


class Store:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # The file is private to the gateway's user before anything is written to it.
        os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
        with closing(sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)) as database:
            database.executescript(SCHEMA)
        with self._transaction() as database:
            for name in ("handle_key", "cursor_key"):
                database.execute(
                    "insert or ignore into meta (key, value) values (?, ?)",
                    (name, secrets.token_hex(32)),
                )

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """One write-locked transaction, so two callers never mint a handle twice."""
        with closing(
            sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
        ) as database:
            database.execute("begin immediate")
            try:
                yield database
            except BaseException:
                database.execute("rollback")
                raise
            database.execute("commit")

    def cursor_key(self) -> bytes:
        """The key page cursors are signed with. It never leaves this database."""
        with closing(sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_SECONDS)) as database:
            return _meta_key(database, "cursor_key")

    def replace_mailbox(self, kind: SourceKind, mailbox: SourceMailbox, synced_at: datetime) -> str:
        """Make the account's stored items match one complete fetch, in one transaction.

        Mail outside the rolling window at `synced_at` is not ingested, and items of
        any account that have left the window are dropped. Returns the account's handle.
        """
        window_start, window_end = window(synced_at)
        with self._transaction() as database:
            key = _meta_key(database, "handle_key")
            account = _handle(database, key, "account", "", mailbox.account_key)
            database.execute("delete from items where account_handle = ?", (account,))
            database.execute("delete from items where received < ?", (window_start,))
            for item in mailbox.items:
                if not window_start <= minute(item.received) <= window_end:
                    continue
                database.execute(
                    "insert or replace into items values (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        _handle(database, key, "item", account, item.upstream_id),
                        account,
                        kind,
                        _handle(database, key, "sender", account, item.sender_key),
                        _handle(database, key, "thread", account, item.thread_key),
                        minute(item.received),
                        item.read_status,
                        int(item.has_attachments),
                    ),
                )
            database.execute(
                "insert or replace into sync_state values (?, ?, 'current', ?)",
                (account, kind, minute(synced_at)),
            )
        return account

    def list_items(
        self,
        account: str,
        now: datetime,
        filters: Filters,
        order: SortOrder,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        """One page of the account's items inside the window, as stored.

        Nothing here vouches for the values. Items of any other account in the
        database, such as one the source served before it was reconfigured, are not
        served.
        """
        where, parameters = _where(account, now, filters)
        with self._reader() as database:
            rows = database.execute(
                "select item_handle, account_handle, source_kind, sender_handle, thread_handle,"
                f" received, read_status, has_attachments from items where {where}"
                f" order by {SORT_ORDERS[order]} limit ? offset ?",
                (*parameters, limit, offset),
            ).fetchall()
        return [
            {
                "item_handle": row[0],
                "account_handle": row[1],
                "source_kind": row[2],
                "sender_handle": row[3],
                "thread_handle": row[4],
                "received": row[5],
                "read_status": row[6],
                # Anything but 0 or 1 stays as it is, for the output validator to refuse.
                "has_attachments": {0: False, 1: True}.get(row[7], row[7]),
            }
            for row in rows
        ]

    def count_items(self, account: str, now: datetime, filters: Filters) -> str:
        """How many items match, as the response spells it: capped at `1000+`."""
        where, parameters = _where(account, now, filters)
        with self._reader() as database:
            # Counting stops one past the cap: no more is ever reported.
            (count,) = database.execute(
                f"select count(*) from (select 1 from items where {where} limit ?)",
                (*parameters, COUNT_CAP + 1),
            ).fetchone()
        return spell_count(count)

    def group_items(
        self,
        account: str,
        now: datetime,
        filters: Filters,
        group_by: GroupBy,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        """One page of group counts, largest first and then by key.

        Groups are ordered by the count as reported, so the order says nothing about
        sizes above the cap.
        """
        where, parameters = _where(account, now, filters)
        column = GROUP_COLUMNS[group_by]
        with self._reader() as database:
            rows = database.execute(
                f"select {column}, count(*) from items where {where} group by {column}"
                f" order by min(count(*), ?) desc, {column} limit ? offset ?",
                (*parameters, COUNT_CAP + 1, limit, offset),
            ).fetchall()
        return [{"key": row[0], "count": spell_count(row[1])} for row in rows]

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self._path, timeout=BUSY_TIMEOUT_SECONDS)) as database:
            yield database


def spell_count(count: int) -> str:
    return COUNT_CAPPED if count > COUNT_CAP else str(count)


def window(now: datetime) -> tuple[str, str]:
    """The first and last minute of the rolling window that ends at `now`, inclusive."""
    return minute(now - timedelta(days=WINDOW_DAYS)), minute(now)


def _where(account: str, now: datetime, filters: Filters) -> tuple[str, list[object]]:
    """The conditions of one query, with their parameters.

    Column names come from this module, never from input; every value is a parameter.
    """
    start, end = window(now)
    conditions = ["account_handle = ?", "received >= ?", "received <= ?"]
    parameters: list[object] = [account, start, end]
    optional: list[tuple[str, object]] = [
        ("read_status = ?", filters.read_status),
        ("received >= ?", filters.received_from),
        ("received <= ?", filters.received_to),
        (
            "has_attachments = ?",
            None if filters.has_attachments is None else int(filters.has_attachments),
        ),
        ("sender_handle = ?", filters.sender_handle),
        ("thread_handle = ?", filters.thread_handle),
        ("account_handle = ?", filters.account_handle),
    ]
    for condition, value in optional:
        if value is not None:
            conditions.append(condition)
            parameters.append(value)
    return " and ".join(conditions), parameters


def _meta_key(database: sqlite3.Connection, name: str) -> bytes:
    row = database.execute("select value from meta where key = ?", (name,)).fetchone()
    return bytes.fromhex(row[0])


def _handle(database: sqlite3.Connection, key: bytes, kind: str, scope: str, upstream: str) -> str:
    """The handle for one upstream entity, minted on first sight and the same ever after."""
    material = "\0".join((kind, scope, upstream)).encode("utf-8", "surrogatepass")
    digest = hmac.new(key, material, hashlib.sha256).hexdigest()
    row = database.execute(
        "select handle from handles where kind = ? and digest = ?", (kind, digest)
    ).fetchone()
    if row is not None:
        handle: str = row[0]
        return handle
    for _ in range(MINT_ATTEMPTS):
        handle = new_handle()
        try:
            database.execute("insert into handles values (?, ?, ?)", (handle, kind, digest))
        except sqlite3.IntegrityError:
            continue  # the handle is taken: draw another
        return handle
    raise RuntimeError("no free handle")
