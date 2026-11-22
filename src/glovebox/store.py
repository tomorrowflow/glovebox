"""SQLite persistence: permitted fields, handle mappings and sync state, nothing else.

Raw text is fetched, reduced to permitted fields and discarded (ADR 0004). The
handle registry therefore does not keep upstream IDs, sender addresses or thread
keys: it keeps a keyed digest of each, with a key that exists only in this
database, which is enough to give the same upstream entity the same handle again.
Handles themselves come from randomness and have no relation to what they stand for.
The same goes for the cache of classifications: an item's text is kept only as a keyed
digest, enough to tell that it changed.
"""

import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, get_args

from glovebox import db
from glovebox.classification import UNKNOWN, Classification
from glovebox.db import MINT_ATTEMPTS, new_handle
from glovebox.fields import ClassificationStatus
from glovebox.formats import (
    COUNT_CAP,
    COUNT_CAPPED,
    SKIPPED_CAP,
    SKIPPED_CAPPED,
    WINDOW_DAYS,
    minute,
)
from glovebox.query import Filters, GroupBy, SortOrder
from glovebox.sources import SourceItem, SourceMailbox, SyncStatus

# What `sync_state.last_sync` holds for an account that has never synced: the column
# cannot be null, and no minute is ever empty.
NEVER_SYNCED = ""

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
    skipped: int = 0  # items the source held that could not be understood


class Store:
    def __init__(self, path: Path) -> None:
        self._path = path
        db.prepare(path)
        with self._transaction() as database:
            for name in ("handle_key", "cursor_key", "content_key"):
                database.execute(
                    "insert or ignore into meta (key, value) values (?, ?)",
                    (name, secrets.token_hex(32)),
                )

    def _transaction(self) -> AbstractContextManager[sqlite3.Connection]:
        return db.transaction(self._path)

    def _reader(self) -> AbstractContextManager[sqlite3.Connection]:
        return db.reader(self._path)

    def cursor_key(self) -> bytes:
        """The key page cursors are signed with. It never leaves this database."""
        with self._reader() as database:
            return _meta_key(database, "cursor_key")

    def prepare_mailbox(
        self, account: str, mailbox: SourceMailbox, synced_at: datetime, config_digest: str
    ) -> PreparedMailbox:
        """Give each in-window item its handle and find which classifications can be reused.

        Mail outside the rolling window at `synced_at` is not ingested. A stored
        classification is reusable when the item's text and the classifier configuration
        (`config_digest`) are what they were when it was made, and it was an answer: an
        `unavailable` one is tried again. Nothing is changed here but the handle registry.
        An account the operator removed in the meantime has no items to prepare.
        """
        window_start, window_end = window(synced_at)
        with self._transaction() as database:
            known = database.execute("select 1 from accounts where handle = ?", (account,))
            if known.fetchone() is None:
                return PreparedMailbox(account, [], mailbox.skipped)
            key = _meta_key(database, "handle_key")
            content_key = _meta_key(database, "content_key")
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
        return PreparedMailbox(account, items, mailbox.skipped)

    def commit_mailbox(
        self,
        kind: str,
        prepared: PreparedMailbox,
        classifications: Sequence[Classification],
        config_digest: str,
        synced_at: datetime,
    ) -> None:
        """Make the account's stored items match one complete fetch, in one transaction.

        `classifications` holds one entry per prepared item, in order. Items of any
        account that have left the window are dropped. The account is current, or
        incomplete if the source held items that could not be understood. An account the
        operator removed in the meantime is not written to.
        """
        window_start, _ = window(synced_at)
        account = prepared.account
        with self._transaction() as database:
            known = database.execute("select 1 from accounts where handle = ?", (account,))
            if known.fetchone() is None:
                return
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
            status: SyncStatus = "incomplete" if prepared.skipped else "current"
            database.execute(
                "insert or replace into sync_state"
                " (account_handle, source_kind, status, last_sync, skipped)"
                " values (?, ?, ?, ?, ?)",
                (account, kind, status, minute(synced_at), prepared.skipped),
            )

    def mark_unsynced(self, account: str, kind: str, status: SyncStatus) -> None:
        """Record that the last sync of the account failed.

        The account keeps its last successful sync time, and its stored items stay as they
        were. An account that never synced is recorded as such. An account the operator
        removed in the meantime is not written to.
        """
        with self._transaction() as database:
            known = database.execute("select 1 from accounts where handle = ?", (account,))
            if known.fetchone() is None:
                return
            database.execute(
                "insert into sync_state (account_handle, source_kind, status, last_sync)"
                " values (?, ?, ?, ?)"
                " on conflict (account_handle) do update set status = excluded.status",
                (account, kind, status, NEVER_SYNCED),
            )

    def account_sync(self, accounts: Sequence[str]) -> list[dict[str, Any]]:
        """Each of `accounts` with its sync status, in the order given, as stored.

        An account that never synced is `stale` with no sync time. Nothing here vouches for
        the values. An account that no longer exists is left out.
        """
        marks = ", ".join("?" for _ in accounts)
        with self._reader() as database:
            rows = database.execute(
                "select accounts.handle, accounts.source_kind, sync_state.status,"
                " sync_state.last_sync, sync_state.skipped from accounts"
                " left join sync_state on sync_state.account_handle = accounts.handle"
                f" where accounts.handle in ({marks})",
                tuple(accounts),
            ).fetchall()
        by_handle = {
            row[0]: {
                "account_handle": row[0],
                "source_kind": row[1],
                "status": "stale" if row[2] is None else row[2],
                "last_sync": None if row[3] in (None, NEVER_SYNCED) else row[3],
                "skipped_items": spell_skipped(0 if row[4] is None else row[4]),
            }
            for row in rows
        }
        return [by_handle[handle] for handle in accounts if handle in by_handle]

    def list_items(
        self,
        accounts: Sequence[str],
        now: datetime,
        filters: Filters,
        order: SortOrder,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        """One page of the items of `accounts` inside the window, as stored.

        Nothing here vouches for the values. Items of any other account in the
        database are not served.
        """
        where, parameters = _where(accounts, now, filters)
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

    def count_items(self, accounts: Sequence[str], now: datetime, filters: Filters) -> str:
        """How many items match, as the response spells it: capped at `1000+`."""
        where, parameters = _where(accounts, now, filters)
        with self._reader() as database:
            # Counting stops one past the cap: no more is ever reported.
            (count,) = database.execute(
                f"select count(*) from (select 1 from items where {where} limit ?)",
                (*parameters, COUNT_CAP + 1),
            ).fetchone()
        return spell_count(count)

    def group_items(
        self,
        accounts: Sequence[str],
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
        where, parameters = _where(accounts, now, filters)
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


def spell_count(count: int) -> str:
    return COUNT_CAPPED if count > COUNT_CAP else str(count)


def spell_skipped(count: object) -> str:
    """How many items were skipped, as the response spells it: capped at `10+`."""
    if isinstance(count, int) and count > SKIPPED_CAP:
        return SKIPPED_CAPPED
    return str(count)


def window(now: datetime) -> tuple[str, str]:
    """The first and last minute of the rolling window that ends at `now`, inclusive."""
    return minute(now - timedelta(days=WINDOW_DAYS)), minute(now)


def _where(accounts: Sequence[str], now: datetime, filters: Filters) -> tuple[str, list[object]]:
    """The conditions of one query, with their parameters.

    Column names come from this module, never from input; every value is a parameter.
    The caller has already narrowed `accounts` by the account handle filter, so that
    filter is not applied again here.
    """
    start, end = window(now)
    marks = ", ".join("?" for _ in accounts)
    conditions = [f"account_handle in ({marks})", "received >= ?", "received <= ?"]
    parameters: list[object] = [*accounts, start, end]
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
