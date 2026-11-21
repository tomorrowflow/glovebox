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
from datetime import datetime
from pathlib import Path
from typing import Any

from glovebox.formats import HANDLE_ALPHABET, HANDLE_LENGTH, MINUTE_FORMAT
from glovebox.sources import SourceKind, SourceMailbox

BUSY_TIMEOUT_SECONDS = 30
MINT_ATTEMPTS = 16

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
            database.execute(
                "insert or ignore into meta (key, value) values ('handle_key', ?)",
                (secrets.token_hex(32),),
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

    def replace_mailbox(self, kind: SourceKind, mailbox: SourceMailbox, synced_at: datetime) -> str:
        """Make the account's stored items match one complete fetch, in one transaction.

        Returns the account's handle.
        """
        with self._transaction() as database:
            key = bytes.fromhex(
                database.execute("select value from meta where key = 'handle_key'").fetchone()[0]
            )
            account = _handle(database, key, "account", "", mailbox.account_key)
            database.execute("delete from items where account_handle = ?", (account,))
            for item in mailbox.items:
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

    def recent_items(self, account: str, limit: int) -> list[dict[str, Any]]:
        """One account's newest items first, as stored. Nothing here vouches for the values.

        Items of any other account in the database, such as one the source served
        before it was reconfigured, are not served.
        """
        with self._transaction() as database:
            rows = database.execute(
                "select item_handle, account_handle, source_kind, sender_handle, thread_handle,"
                " received, read_status, has_attachments from items where account_handle = ?"
                " order by received desc, item_handle limit ?",
                (account, limit),
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
