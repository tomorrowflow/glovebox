"""The SQLite file: its schema and the two ways of opening it.

The gate and the operator's command line open the same file, each for as long as one
statement or transaction needs.
"""

import os
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from glovebox.formats import HANDLE_ALPHABET, HANDLE_LENGTH

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
create table if not exists accounts (
    handle text primary key,
    name text not null unique,
    source_kind text not null,
    source text not null,
    credentials_file text
);
create table if not exists tokens (
    id text primary key,
    digest text not null unique,
    label text not null,
    created text not null,
    revoked text
);
create table if not exists token_accounts (
    token_id text not null references tokens (id),
    account_handle text not null references accounts (handle),
    primary key (token_id, account_handle)
);
create table if not exists token_operations (
    token_id text not null references tokens (id),
    operation text not null,
    primary key (token_id, operation)
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
create table if not exists events (
    event_handle text primary key,
    account_handle text not null,
    start_time text not null,
    end_time text not null,
    all_day integer not null,
    busy_status text not null
);
create index if not exists events_by_start on events (start_time, event_handle);
create table if not exists sync_state (
    account_handle text primary key,
    source_kind text not null,
    status text not null,
    last_sync text not null,
    skipped integer not null default 0
);
"""


def new_handle() -> str:
    return "".join(secrets.choice(HANDLE_ALPHABET) for _ in range(HANDLE_LENGTH))


def prepare(path: Path) -> None:
    """Create the database if it is missing, private to the user, and its tables."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # The file is private to the gateway's user before anything is written to it.
    os.close(os.open(path, os.O_RDWR | os.O_CREAT, 0o600))
    with closing(sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)) as database:
        _drop_items_without_semantic_fields(database)
        database.executescript(SCHEMA)
        # A database from before sync counts: add the column the schema now has.
        columns = {row[1] for row in database.execute("pragma table_info(sync_state)")}
        if "skipped" not in columns:
            database.execute("alter table sync_state add column skipped integer not null default 0")


def _drop_items_without_semantic_fields(database: sqlite3.Connection) -> None:
    """Items are a copy of what the source serves, rebuilt on every call, so a table from
    before the semantic fields is dropped rather than migrated."""
    columns = {row[1] for row in database.execute("pragma table_info(items)")}
    if columns and "classification_status" not in columns:
        database.execute("drop table items")


@contextmanager
def reader(path: Path) -> Iterator[sqlite3.Connection]:
    with closing(sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS)) as database:
        yield database


@contextmanager
def transaction(path: Path) -> Iterator[sqlite3.Connection]:
    """One write-locked transaction, so two callers never mint a handle twice."""
    with closing(
        sqlite3.connect(path, timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None)
    ) as database:
        database.execute("begin immediate")
        try:
            yield database
        except BaseException:
            database.execute("rollback")
            raise
        database.execute("commit")
