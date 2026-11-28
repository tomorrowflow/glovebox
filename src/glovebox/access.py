"""Accounts and agent tokens: what the operator sets up and what a token may reach.

Only the operator's local command line writes here (ADR 0004); the service reads it on
every request, so a revoked token or a removed account stops working at once.

A token value is shown once, when it is issued. The database keeps its SHA-256 digest,
which cannot be presented as a token. The values are 256-bit random strings, so a fast
hash is enough: there is nothing to guess and nothing to stretch.
"""

import hashlib
import os
import re
import secrets
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from glovebox import db
from glovebox.db import MINT_ATTEMPTS, new_handle
from glovebox.formats import MAX_ACCOUNTS, MINUTE_FORMAT
from glovebox.sources import CREDENTIALS_REQUIRED, SOURCE_KINDS

LIST_ACCOUNTS_OPERATION = "list_accounts"
LIST_OPERATION = "list_items"
COUNT_OPERATION = "count_items"
GROUP_OPERATION = "group_items"
LIST_TOPICS_OPERATION = "list_topics"
AVAILABILITY_OPERATION = "get_availability"
LIST_EVENTS_OPERATION = "list_events"
LIST_OVERLAPS_OPERATION = "list_overlaps"
# The read operations a token can be limited to; one name per read tool.
OPERATIONS = (
    LIST_ACCOUNTS_OPERATION,
    LIST_OPERATION,
    COUNT_OPERATION,
    GROUP_OPERATION,
    LIST_TOPICS_OPERATION,
    AVAILABILITY_OPERATION,
    LIST_EVENTS_OPERATION,
    LIST_OVERLAPS_OPERATION,
)

ACCOUNT_NAME = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
LABEL = re.compile(r"[A-Za-z0-9 ._-]{0,64}")
TOKEN_PREFIX = "gbx_"
MIN_SUPPLIED_TOKEN_LENGTH = 32
TOKEN_PATTERN = re.compile(r"[\x21-\x7e]{1,512}")  # printable ASCII, no whitespace


class AccessError(Exception):
    """The operator asked for something that cannot be done. The message may repeat
    what the operator typed, and never a token or a credential."""


@dataclass(frozen=True)
class Account:
    name: str  # the operator's own label; agents never see it
    handle: str
    kind: str
    source: str
    credentials: Path | None


@dataclass(frozen=True)
class TokenRecord:
    id: str
    label: str
    created: str
    revoked: str | None
    operations: tuple[str, ...]
    accounts: tuple[str, ...]  # account names


@dataclass(frozen=True)
class Grant:
    """What an authenticated token may reach: its read operations and its accounts."""

    token_id: str
    operations: frozenset[str]
    accounts: tuple[Account, ...]


def digest_of(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Access:
    def __init__(self, path: Path) -> None:
        self._path = path
        db.prepare(path)

    # --- accounts (operator) -----------------------------------------------------------

    def add_account(
        self, name: str, kind: str, source: str, credentials: Path | None = None
    ) -> Account:
        if not ACCOUNT_NAME.fullmatch(name):
            raise AccessError(
                "an account name is 1 to 63 characters: lowercase letters, digits, '.', '_' or "
                "'-', starting with a letter or digit"
            )
        if kind not in SOURCE_KINDS:
            raise AccessError(f"unknown source kind, expected one of: {', '.join(SOURCE_KINDS)}")
        if not source or not source.isprintable():
            raise AccessError("the source must be a file path or a URL")
        if kind in CREDENTIALS_REQUIRED and credentials is None:
            raise AccessError(f"source kind {kind} needs a credentials file")
        # Made absolute without following symlinks: a symlinked secret stays one.
        stored = None if credentials is None else Path(os.path.abspath(credentials))
        with db.transaction(self._path) as database:
            for _ in range(MINT_ATTEMPTS):
                handle = new_handle()
                try:
                    database.execute(
                        "insert into accounts values (?, ?, ?, ?, ?)",
                        (handle, name, kind, source, None if stored is None else str(stored)),
                    )
                except sqlite3.IntegrityError:
                    if _account_named(database, name) is not None:
                        raise AccessError(f"an account named {name} exists already") from None
                    continue  # the handle is taken: draw another
                return Account(name, handle, kind, source, stored)
        raise AccessError("no free account handle")

    def accounts(self) -> list[Account]:
        with db.reader(self._path) as database:
            rows = database.execute(
                "select name, handle, source_kind, source, credentials_file from accounts"
                " order by name"
            ).fetchall()
        return [_account(row) for row in rows]

    def set_source(self, name: str, source: str) -> None:
        """Point an existing account at another source; its handle and tokens stay.

        What was last synced came from the old source, so the account is stale until it
        is read from the new one.
        """
        with db.transaction(self._path) as database:
            account = _account_named(database, name)
            if account is None:
                raise AccessError(f"no account named {name}")
            database.execute("update accounts set source = ? where name = ?", (source, name))
            database.execute(
                "update sync_state set status = 'stale' where account_handle = ?",
                (account.handle,),
            )

    def remove_account(self, name: str) -> None:
        """Remove an account, the items stored for it and its place in every token's scope."""
        with db.transaction(self._path) as database:
            account = _account_named(database, name)
            if account is None:
                raise AccessError(f"no account named {name}")
            for table in ("items", "events", "sync_state", "token_accounts"):
                database.execute(f"delete from {table} where account_handle = ?", (account.handle,))
            database.execute("delete from accounts where handle = ?", (account.handle,))

    # --- tokens (operator) -------------------------------------------------------------

    def issue_token(
        self,
        accounts: Sequence[str],
        operations: Sequence[str] = OPERATIONS,
        label: str = "",
        value: str | None = None,
        now: datetime | None = None,
    ) -> str:
        """Issue a token for `accounts` and `operations` and return its value, once.

        A caller that already holds a value (the fixture bootstrap) passes it in; the
        database stores its digest either way.
        """
        names = sorted(set(accounts))
        permitted = [operation for operation in OPERATIONS if operation in set(operations)]
        if not names:
            raise AccessError("a token needs at least one account")
        if len(names) > MAX_ACCOUNTS:
            raise AccessError(f"a token covers at most {MAX_ACCOUNTS} accounts")
        unknown = sorted(set(operations) - set(OPERATIONS))
        if unknown or not permitted:
            raise AccessError(f"operations are chosen from: {', '.join(OPERATIONS)}")
        if not LABEL.fullmatch(label):
            raise AccessError("a label is up to 64 letters, digits, spaces, '.', '_' or '-'")
        if value is None:
            value = TOKEN_PREFIX + secrets.token_urlsafe(32)
        elif len(value) < MIN_SUPPLIED_TOKEN_LENGTH or not TOKEN_PATTERN.fullmatch(value):
            raise AccessError(
                f"a token is at least {MIN_SUPPLIED_TOKEN_LENGTH} printable characters "
                "without whitespace"
            )
        created = (now or datetime.now(UTC)).strftime(MINUTE_FORMAT)
        with db.transaction(self._path) as database:
            handles = []
            for name in names:
                account = _account_named(database, name)
                if account is None:
                    raise AccessError(f"no account named {name}")
                handles.append(account.handle)
            token_id = self._insert_token(database, value, label, created)
            for handle in handles:
                database.execute("insert into token_accounts values (?, ?)", (token_id, handle))
            for operation in permitted:
                database.execute(
                    "insert into token_operations values (?, ?)", (token_id, operation)
                )
        return value

    def _insert_token(
        self, database: sqlite3.Connection, value: str, label: str, created: str
    ) -> str:
        digest = digest_of(value)
        for _ in range(MINT_ATTEMPTS):
            token_id = new_handle()
            try:
                database.execute(
                    "insert into tokens values (?, ?, ?, ?, null)",
                    (token_id, digest, label, created),
                )
            except sqlite3.IntegrityError:
                if database.execute("select 1 from tokens where digest = ?", (digest,)).fetchone():
                    raise AccessError("that token value is already in use") from None
                continue  # the id is taken: draw another
            return token_id
        raise AccessError("no free token id")

    def tokens(self) -> list[TokenRecord]:
        with db.reader(self._path) as database:
            rows = database.execute(
                "select id, label, created, revoked from tokens order by created, id"
            ).fetchall()
            records = []
            for token_id, label, created, revoked in rows:
                operations = [
                    row[0]
                    for row in database.execute(
                        "select operation from token_operations where token_id = ?", (token_id,)
                    )
                ]
                names = [
                    row[0]
                    for row in database.execute(
                        "select accounts.name from token_accounts join accounts"
                        " on accounts.handle = token_accounts.account_handle"
                        " where token_id = ? order by accounts.name",
                        (token_id,),
                    )
                ]
                records.append(
                    TokenRecord(
                        token_id,
                        label,
                        created,
                        revoked,
                        tuple(op for op in OPERATIONS if op in operations),
                        tuple(names),
                    )
                )
        return records

    def revoke_token(self, token_id: str, now: datetime | None = None) -> bool:
        """Revoke a token; True if it was active. Revoking a revoked token changes nothing."""
        revoked = (now or datetime.now(UTC)).strftime(MINUTE_FORMAT)
        with db.transaction(self._path) as database:
            row = database.execute(
                "select revoked from tokens where id = ?", (token_id,)
            ).fetchone()
            if row is None:
                # Not repeated: what was typed may be a token value pasted by mistake.
                raise AccessError("no token has that id; `token list` shows the ids")
            if row[0] is not None:
                return False
            database.execute("update tokens set revoked = ? where id = ?", (revoked, token_id))
            return True

    def ensure_fixture(self, name: str, kind: str, source: str, token: str) -> bool:
        """Make the fixture stack's account and token exist, and leave the rest alone.

        Safe to repeat at every start. The account follows `source`. A token that is
        already registered stays as it is, so one the operator revoked stays revoked.
        Returns True if the token was registered now.
        """
        with db.reader(self._path) as database:
            account = _account_named(database, name)
        if account is None:
            self.add_account(name, kind, source)
        elif account.source != source:
            self.set_source(name, source)
        with db.transaction(self._path) as database:
            known = database.execute(
                "select id, revoked from tokens where digest = ?", (digest_of(token),)
            ).fetchone()
            if known is not None:
                if known[1] is None:
                    # An account added again after the operator removed it is covered again.
                    database.execute(
                        "insert or ignore into token_accounts"
                        " select ?, handle from accounts where name = ?",
                        (known[0], name),
                    )
                return False
        self.issue_token([name], label="fixture", value=token)
        return True

    # --- the service -------------------------------------------------------------------

    def authenticate(self, presented: str) -> str | None:
        """The id of the active token with this value, else None."""
        digest = digest_of(presented)
        with db.reader(self._path) as database:
            row = database.execute(
                "select id from tokens where digest = ? and revoked is null", (digest,)
            ).fetchone()
        return None if row is None else str(row[0])

    def grant(self, token_id: str) -> Grant | None:
        """What an active token may reach, as of now; None if it is revoked or unknown."""
        with db.reader(self._path) as database:
            active = database.execute(
                "select 1 from tokens where id = ? and revoked is null", (token_id,)
            ).fetchone()
            if active is None:
                return None
            operations = frozenset(
                row[0]
                for row in database.execute(
                    "select operation from token_operations where token_id = ?", (token_id,)
                )
            )
            rows = database.execute(
                "select accounts.name, accounts.handle, accounts.source_kind, accounts.source,"
                " accounts.credentials_file from token_accounts join accounts"
                " on accounts.handle = token_accounts.account_handle"
                " where token_id = ? order by accounts.name",
                (token_id,),
            ).fetchall()
        return Grant(token_id, operations, tuple(_account(row) for row in rows))


def _account(row: Sequence[object]) -> Account:
    name, handle, kind, source, credentials = row
    return Account(
        str(name),
        str(handle),
        str(kind),
        str(source),
        None if credentials is None else Path(str(credentials)),
    )


def _account_named(database: sqlite3.Connection, name: str) -> Account | None:
    row = database.execute(
        "select name, handle, source_kind, source, credentials_file from accounts where name = ?",
        (name,),
    ).fetchone()
    return None if row is None else _account(row)
