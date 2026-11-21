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
from collections.abc import Iterator, Sequence
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, get_args

from glovebox.classification import UNKNOWN, Classification
from glovebox.fields import ClassificationStatus
from glovebox.formats import (
    COUNT_CAP,
    COUNT_CAPPED,
    HANDLE_ALPHABET,
    HANDLE_LENGTH,
    WINDOW_DAYS,
    minute,
)
from glovebox.query import Filters, GroupBy, SortOrder
from glovebox.sources import SourceItem, SourceKind, SourceMailbox

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
    "topic": "topic",
    "reply_needed": "reply_needed",
    "action_requested": "action_requested",
    "deadline": "deadline",
    "classification_status": "classification_status",
}
STATUSES = get_args(ClassificationStatus)

ITEM_COLUMN_COUNT = 15  # the columns of the items table, in the order it declares them

# The columns of an item row that the classifier decides, with its status.
SEMANTIC_COLUMNS = "topic, reply_needed, action_requested, deadline, classification_status"

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
    has_attachments integer not null,
    topic text not null,
    reply_needed text not null,
    action_requested text not null,
    deadline text,
    classification_status text not null,
    content_digest text not null,
    config_digest text not null
);
create index if not exists items_by_received on items (received desc, item_handle);
create table if not exists sync_state (
    account_handle text primary key,
    source_kind text not null,
    status text not null,
    last_sync text not null
);
"""


@dataclass(frozen=True)
class PreparedItem:
    """An in-window item with its handle, a keyed digest of its text and, if a classification
    stored for exactly this text under the current configuration can be reused, that one."""

    item_handle: str
    item: SourceItem
    content_digest: str
    cached: Classification | None


@dataclass(frozen=True)
class PreparedMailbox:
    account: str
    items: list[PreparedItem]


def new_handle() -> str:
    return "".join(secrets.choice(HANDLE_ALPHABET) for _ in range(HANDLE_LENGTH))


class Store:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        # The file is private to the gateway's user before anything is written to it.
        os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
        with closing(sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)) as database:
            _drop_items_without_semantic_fields(database)
            database.executescript(SCHEMA)
        with self._transaction() as database:
            for name in ("handle_key", "cursor_key", "content_key"):
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

    def prepare_mailbox(
        self, mailbox: SourceMailbox, synced_at: datetime, config_digest: str
    ) -> PreparedMailbox:
        """Give each in-window item its handle and find which classifications can be reused.

        Mail outside the rolling window at `synced_at` is not ingested. A stored
        classification is reusable when the item's text and the classifier configuration
        (`config_digest`) are what they were when it was made, and it was an answer: an
        `unavailable` one is tried again. Nothing is changed here but the handle registry.
        """
        window_start, window_end = window(synced_at)
        with self._transaction() as database:
            key = _meta_key(database, "handle_key")
            content_key = _meta_key(database, "content_key")
            account = _handle(database, key, "account", "", mailbox.account_key)
            stored = {
                row[0]: row
                for row in database.execute(
                    f"select item_handle, content_digest, config_digest, {SEMANTIC_COLUMNS}"
                    " from items where account_handle = ?",
                    (account,),
                )
            }
            items: list[PreparedItem] = []
            for item in mailbox.items:
                if not window_start <= minute(item.received) <= window_end:
                    continue
                handle = _handle(database, key, "item", account, item.upstream_id)
                digest = _content_digest(content_key, item.text)
                items.append(
                    PreparedItem(
                        handle, item, digest, _reusable(stored.get(handle), digest, config_digest)
                    )
                )
        return PreparedMailbox(account, items)

    def commit_mailbox(
        self,
        kind: SourceKind,
        prepared: PreparedMailbox,
        classifications: Sequence[Classification],
        config_digest: str,
        synced_at: datetime,
    ) -> str:
        """Make the account's stored items match one complete fetch, in one transaction.

        `classifications` holds one entry per prepared item, in order. Items of any
        account that have left the window are dropped. Returns the account's handle.
        """
        window_start, _ = window(synced_at)
        account = prepared.account
        with self._transaction() as database:
            key = _meta_key(database, "handle_key")
            database.execute("delete from items where account_handle = ?", (account,))
            database.execute("delete from items where received < ?", (window_start,))
            for prepared_item, classification in zip(prepared.items, classifications, strict=True):
                item = prepared_item.item
                database.execute(
                    f"insert or replace into items values ({', '.join('?' * ITEM_COLUMN_COUNT)})",
                    (
                        prepared_item.item_handle,
                        account,
                        kind,
                        _handle(database, key, "sender", account, item.sender_key),
                        _handle(database, key, "thread", account, item.thread_key),
                        minute(item.received),
                        item.read_status,
                        int(item.has_attachments),
                        classification.topic,
                        classification.reply_needed,
                        classification.action_requested,
                        classification.deadline,
                        classification.status,
                        prepared_item.content_digest,
                        config_digest,
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
                f" received, read_status, has_attachments, {SEMANTIC_COLUMNS} from items"
                f" where {where}"
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
                "topic": row[8],
                "reply_needed": row[9],
                "action_requested": row[10],
                "deadline": UNKNOWN if row[11] is None else row[11],
                "classification_status": row[12],
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
        # A deadline that is not known is stored as no value and spelled `unknown`.
        return [
            {"key": UNKNOWN if row[0] is None else row[0], "count": spell_count(row[1])}
            for row in rows
        ]

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
        ("topic = ?", filters.topic),
        ("reply_needed = ?", filters.reply_needed),
        ("action_requested = ?", filters.action_requested),
        # An unknown deadline is stored as no value, which no range matches.
        ("deadline >= ?", filters.deadline_from),
        ("deadline <= ?", filters.deadline_to),
        ("classification_status = ?", filters.classification_status),
    ]
    for condition, value in optional:
        if value is not None:
            conditions.append(condition)
            parameters.append(value)
    return " and ".join(conditions), parameters


def _drop_items_without_semantic_fields(database: sqlite3.Connection) -> None:
    """Items are a copy of what the source serves, rebuilt on every call, so a table from
    before the semantic fields is dropped rather than migrated."""
    columns = {row[1] for row in database.execute("pragma table_info(items)")}
    if columns and "classification_status" not in columns:
        database.execute("drop table items")


def _content_digest(key: bytes, text: str) -> str:
    """A keyed digest of an item's text: enough to tell that it changed, not to recover or
    guess it."""
    return hmac.new(key, text.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()


def _reusable(row: Any, content_digest: str, config_digest: str) -> Classification | None:
    """The classification in a stored item row, if it answers for this text and configuration."""
    if row is None or (row[1], row[2]) != (content_digest, config_digest):
        return None
    topic, reply_needed, action_requested, deadline, status = row[3:8]
    if status not in STATUSES or status == "unavailable":
        return None
    return Classification(topic, reply_needed, action_requested, deadline, status)


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
