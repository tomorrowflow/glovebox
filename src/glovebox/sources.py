"""Source adapters: the only code that sees raw upstream structure.

An adapter fetches one account's mail and hands the gate normalized records. A
record holds the upstream keys the handle registry needs and the few values that
become permitted fields; subjects, bodies, names, recipients, filenames and every
other upstream value are never read out of the document (ADR 0004).
"""

import http.client
import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parseaddr
from pathlib import Path
from typing import Literal, Protocol

ReadStatus = Literal["read", "unread", "unknown"]
SourceKind = Literal["fixture_mail"]

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 10


class SourceError(Exception):
    """The source could not be read or understood. It carries no detail on purpose:
    anything an upstream said stays out of exceptions, logs and responses."""


@dataclass(frozen=True)
class SourceItem:
    """One mail item as the gate keeps it: upstream keys and values that map to permitted fields."""

    upstream_id: str
    sender_key: str
    thread_key: str
    received: datetime  # UTC, truncated to the minute
    read_status: ReadStatus
    has_attachments: bool


@dataclass(frozen=True)
class SourceMailbox:
    account_key: str
    items: list[SourceItem]


class MailSource(Protocol):
    kind: SourceKind

    def fetch(self) -> SourceMailbox:
        """Fetch the account's recent mail. Raises SourceError, never anything with detail."""
        ...


class FixtureMailSource:
    """Mail from a fixture document, read from a file or from a stand-in over HTTP.

    The document has the shape the stand-ins serve: an `account` and a list of
    `messages`. Each message needs `id`, `from` and `received`; `thread`, `read` and
    `attachments` are optional. A message that cannot be understood is skipped.
    """

    kind: SourceKind = "fixture_mail"

    def __init__(self, location: Path | str) -> None:
        self._location = location

    def fetch(self) -> SourceMailbox:
        try:
            return parse_mailbox(json.loads(self._read()))
        except (
            OSError,
            ValueError,  # includes bad JSON and bad UTF-8
            RecursionError,
            urllib.error.URLError,
            http.client.HTTPException,
        ):
            raise SourceError from None

    def _read(self) -> bytes:
        if isinstance(self._location, Path):
            with self._location.open("rb") as handle:
                data = handle.read(MAX_DOCUMENT_BYTES + 1)
        else:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(self._location, timeout=FETCH_TIMEOUT_SECONDS) as response:
                data = response.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) > MAX_DOCUMENT_BYTES:
            raise SourceError
        return data


def parse_mailbox(document: object) -> SourceMailbox:
    if not isinstance(document, Mapping):
        raise SourceError
    account = document.get("account")
    messages = document.get("messages")
    if not isinstance(account, str) or not account or not isinstance(messages, list):
        raise SourceError
    items: dict[str, SourceItem] = {}
    for message in messages:
        item = parse_message(message)
        if item is not None and item.upstream_id not in items:
            items[item.upstream_id] = item
    return SourceMailbox(account_key=account, items=list(items.values()))


def parse_message(message: object) -> SourceItem | None:
    if not isinstance(message, Mapping):
        return None
    upstream_id = message.get("id")
    sender = message.get("from")
    received = parse_received(message.get("received"))
    if not isinstance(upstream_id, str) or not upstream_id or received is None:
        return None
    sender_key = sender_key_of(sender)
    if sender_key is None:
        return None
    thread = message.get("thread")
    read = message.get("read")
    attachments = message.get("attachments")
    return SourceItem(
        upstream_id=upstream_id,
        sender_key=sender_key,
        thread_key=thread if isinstance(thread, str) and thread else upstream_id,
        received=received,
        read_status="unknown" if not isinstance(read, bool) else "read" if read else "unread",
        has_attachments=isinstance(attachments, list) and len(attachments) > 0,
    )


def sender_key_of(sender: object) -> str | None:
    """The address of a From value, lowercased, so one address is one sender."""
    if not isinstance(sender, str):
        return None
    address = parseaddr(sender)[1] or sender
    key = address.strip().lower()
    return key or None


def parse_received(value: object) -> datetime | None:
    """A timezone-aware ISO 8601 time as UTC at minute precision, else None."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(UTC).replace(second=0, microsecond=0)
    except (ValueError, OverflowError):
        return None
