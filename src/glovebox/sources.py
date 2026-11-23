"""Source adapters: the only code that sees raw upstream structure.

An adapter fetches one account's mail or messenger conversations and hands the gate
normalized records. A record holds the upstream keys the handle registry needs and the few
values that become permitted fields; subjects, names, group names, recipients, reactions,
quotes, previews, filenames, media types and every other upstream value are never read out
of the document (ADR 0004). The one exception is an item's text, its subject and body or its
message text and caption: it is read so that the quarantined classifier can judge it, held in
memory for that, and never stored, logged or served.
"""

import http.client
import ipaddress
import json
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parseaddr
from pathlib import Path
from typing import Literal, Protocol, get_args
from urllib.parse import urlsplit

from glovebox import chatdb
from glovebox.credentials import Secret, read_credentials

ReadStatus = Literal["read", "unread", "unknown"]
SourceKind = Literal["fixture_mail", "fixture_messenger", "imessage"]
# Who stops a write to the source. `provider`: the access method cannot write at all, so the
# gate could not write if it tried. `gateway`: the access method could write and the gate has
# no code path that does (ADR 0003). No source has a provider-enforced read-only access yet.
ReadOnlyEnforcement = Literal["gateway", "provider"]
# How fresh and complete an account's data is. `current`: the last sync read the whole
# source. `incomplete`: it did, but some items could not be understood and are missing.
# `stale`: the last sync failed, so the data is from an earlier one. `blocked`: the last
# sync failed because access is not granted.
SyncStatus = Literal["current", "stale", "incomplete", "blocked"]
SOURCE_KINDS: tuple[str, ...] = get_args(SourceKind)
READ_ONLY_ENFORCEMENT: dict[str, ReadOnlyEnforcement] = dict.fromkeys(SOURCE_KINDS, "gateway")
# Kinds whose account must name a credentials file. The fixture kinds and the iMessage
# source (a local file, or a stand-in that asks for none) need none.
CREDENTIALS_REQUIRED: frozenset[str] = frozenset()

MAX_DOCUMENT_BYTES = 4 * 1024 * 1024
FETCH_TIMEOUT_SECONDS = 10
MAX_TEXT_CHARS = 16_000  # what the classifier is shown of one item; the rest is cut off


# HTTP statuses that mean access to the source is not granted, as opposed to it being down.
BLOCKED_HTTP_STATUSES = frozenset({401, 403})


class SourceError(Exception):
    """The source could not be read or understood. It carries no detail on purpose:
    anything an upstream said stays out of exceptions, logs and responses."""


class SourceBlocked(SourceError):
    """The source refused access: credentials or permissions are missing, not the network."""


@dataclass(frozen=True)
class SourceItem:
    """One mail or messenger item as the gate keeps it: upstream keys and values that map to
    permitted fields."""

    upstream_id: str
    sender_key: str
    thread_key: str  # a mail thread, or a messenger conversation
    received: datetime  # UTC, truncated to the minute
    read_status: ReadStatus
    has_attachments: bool
    # The subject and body, for the classifier only. Kept out of repr so that an item that
    # ends up in a log line or a traceback does not carry it.
    text: str = field(default="", repr=False)


@dataclass(frozen=True)
class SourceMailbox:
    account_key: str
    items: list[SourceItem]
    skipped: int = 0  # items that could not be understood and were left out


class ItemSource(Protocol):
    kind: SourceKind

    def fetch(self, since: datetime, now: datetime) -> SourceMailbox:
        """Fetch the account's recent items. `since` (UTC) is the start of the retrieval
        window: a source may leave out anything older, and the gate drops it anyway. `now`
        is the gate's clock, for a source that judges its own freshness.

        Raises SourceBlocked if access is not granted and SourceError for any other
        failure, never anything with detail.
        """
        ...


class NoRedirects(urllib.request.HTTPRedirectHandler):
    """A redirect is a failure: a request that carries a credential must not be sent on."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class FixtureSource(ABC):
    """A document, read from a file or from a stand-in or bridge over HTTP, and the way an
    unreadable one is told apart from one that refuses access. A subclass names the kind
    and parses the document. Over HTTP the one request is a GET of the location, carrying
    the bearer token if there is one."""

    kind: SourceKind

    def __init__(self, location: Path | str, bearer: Secret | None = None) -> None:
        self._location = location
        self._bearer = bearer

    @abstractmethod
    def parse(self, document: object) -> SourceMailbox: ...

    def fetch(self, since: datetime, now: datetime) -> SourceMailbox:
        try:
            return self.parse(json.loads(self._read()))
        except SourceError:
            raise
        except urllib.error.HTTPError as error:
            error.close()
            if error.code in BLOCKED_HTTP_STATUSES:
                raise SourceBlocked from None
            raise SourceError from None
        except PermissionError:
            raise SourceBlocked from None
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
            request = urllib.request.Request(self._location, method="GET")
            if self._bearer is not None:
                if not bearer_may_travel(self._location):
                    raise SourceBlocked
                request.add_header("Authorization", f"Bearer {self._bearer.reveal()}")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirects)
            with opener.open(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
                data = response.read(MAX_DOCUMENT_BYTES + 1)
        if len(data) > MAX_DOCUMENT_BYTES:
            raise SourceError
        return data


class FixtureMailSource(FixtureSource):
    """Mail from a fixture document.

    The document has the shape the stand-ins serve: an `account` and a list of
    `messages`. Each message needs `id`, `from` and `received`; `thread`, `read` and
    `attachments` are optional. A message that cannot be understood is skipped and
    counted.
    """

    kind: SourceKind = "fixture_mail"

    def parse(self, document: object) -> SourceMailbox:
        return parse_mailbox(document)


class FixtureMessengerSource(FixtureSource):
    """Messenger conversations from a fixture document: the pattern of the live messengers.

    The document has the shape the messenger stand-ins serve: an `account` and a list of
    `conversations`. A conversation needs an `id` and its `messages`, direct or group alike;
    its other keys (participants, names, topics) are never read. A message needs `from` and
    `sent`; `id`, `read`, `media`, `text` and `caption` are optional. A conversation or
    message that cannot be understood is skipped and counted.
    """

    kind: SourceKind = "fixture_messenger"

    def parse(self, document: object) -> SourceMailbox:
        return parse_conversations(document)


def is_loopback(location: str) -> bool:
    """Whether an http(s) location is on this host."""
    host = urlsplit(location).hostname
    if host == "localhost":
        return True
    try:
        return host is not None and ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def bearer_may_travel(location: str) -> bool:
    """A bridge token goes only over TLS or to this host, never in clear across a network."""
    return urlsplit(location).scheme == "https" or is_loopback(location)


def bridge_problem(location: str, has_credentials: bool) -> str | None:
    """Why a bridge at this http(s) location may not be used, in fixed text, or None.

    A bridge on another host is a separate device: the connection to it is authenticated
    (ADR 0004, ticket 19), so it needs a credentials file holding its bearer token, and the
    token goes only over TLS. Only the stand-in and a bridge on this host may go without.
    """
    if is_loopback(location):
        return None
    if not has_credentials:
        return "a bridge on another host needs --credentials, a file holding its bearer token"
    if not bearer_may_travel(location):
        return "the bridge token is sent only over https or to this host: use an https source"
    return None


class IMessageSource(FixtureSource):
    """iMessage conversations, from the Messages database or from a bridge.

    A file location is the Messages database (`chat.db`), read in place and read-only; see
    `glovebox.chatdb`. An http(s) location is a bridge, such as the test tier's stand-in,
    that answers a GET with the messenger document of `FixtureMessengerSource`; the account's
    credentials file, if it has one, holds the bearer token the bridge asks for. Only received
    messages are items: the operator's own, reactions and system messages are not.
    """

    kind: SourceKind = "imessage"

    def __init__(
        self,
        location: Path | str,
        bearer: Secret | None = None,
        max_silence: timedelta | None = None,
    ) -> None:
        super().__init__(location, bearer)
        # For the local database only: a bridge answers for its own freshness.
        self._max_silence = max_silence

    def parse(self, document: object) -> SourceMailbox:
        return parse_conversations(document)

    def fetch(self, since: datetime, now: datetime) -> SourceMailbox:
        if isinstance(self._location, Path):
            return self._read_database(self._location, since, now)
        if bridge_problem(self._location, self._bearer is not None) is not None:
            # Also enforced when the account is added; this holds if the stored source or
            # credentials changed since. Nothing is sent: access is not granted.
            raise SourceBlocked
        return super().fetch(since, now)

    def _read_database(self, path: Path, since: datetime, now: datetime) -> SourceMailbox:
        try:
            found = chatdb.read_messages(path, since, now=now, max_silence=self._max_silence)
        except chatdb.ChatDbBlocked:
            raise SourceBlocked from None
        except chatdb.ChatDbError:
            raise SourceError from None
        items: dict[str, SourceItem] = {}
        skipped = found.skipped
        for message in found.messages:
            sender_key = chat_sender_key_of(message.sender)
            if sender_key is None:
                skipped += 1
                continue
            items.setdefault(
                message.guid,
                SourceItem(
                    upstream_id=message.guid,
                    sender_key=sender_key,
                    thread_key=message.chat,
                    received=message.received.replace(second=0, microsecond=0),
                    read_status=read_status_of(message.is_read),
                    has_attachments=message.has_attachments,
                    text=message.text[:MAX_TEXT_CHARS],
                ),
            )
        return SourceMailbox(account_key="imessage", items=list(items.values()), skipped=skipped)


def parse_conversations(document: object) -> SourceMailbox:
    if not isinstance(document, Mapping):
        raise SourceError
    account = document.get("account")
    conversations = document.get("conversations")
    if not isinstance(account, str) or not account or not isinstance(conversations, list):
        raise SourceError
    items: dict[str, SourceItem] = {}
    skipped = 0
    for conversation in conversations:
        key = conversation.get("id") if isinstance(conversation, Mapping) else None
        messages = conversation.get("messages") if isinstance(conversation, Mapping) else None
        if not isinstance(key, str) or not key or not isinstance(messages, list):
            # What a conversation held cannot be told, so it counts for what it declared.
            skipped += len(messages) if isinstance(messages, list) else 1
            continue
        repeats: dict[tuple[object, object], int] = {}
        for message in messages:
            item = parse_chat_message(key, message, repeats)
            if item is None:
                skipped += 1
            elif item.upstream_id not in items:
                items[item.upstream_id] = item
    return SourceMailbox(account_key=account, items=list(items.values()), skipped=skipped)


def parse_chat_message(
    conversation: str, message: object, repeats: dict[tuple[object, object], int]
) -> SourceItem | None:
    """One message of a conversation. A message without an ID is keyed by who sent it and
    when, and by how many alike came before it, so that it keeps its handle."""
    if not isinstance(message, Mapping):
        return None
    sent = message.get("sent")
    received = parse_received(sent)
    sender_key = chat_sender_key_of(message.get("from"))
    if received is None or sender_key is None:
        return None
    message_id = message.get("id")
    if isinstance(message_id, str) and message_id:
        upstream_id = f"{conversation}\0{message_id}"
    else:
        position = (sender_key, sent)
        repeat = repeats.get(position, 0)
        repeats[position] = repeat + 1
        upstream_id = f"{conversation}\0{sender_key}\0{sent}\0{repeat}"
    media = message.get("media")
    return SourceItem(
        upstream_id=upstream_id,
        sender_key=sender_key,
        thread_key=conversation,
        received=received,
        read_status=read_status_of(message.get("read")),
        # Presence only: what the media is, and what it is called, is never read.
        has_attachments=isinstance(media, list) and len(media) > 0,
        text=chat_text_of(message),
    )


def chat_text_of(message: Mapping[str, object]) -> str:
    """The message text and caption, those of them that are strings, cut to the limit."""
    parts = [
        part
        for part in (message.get("text"), message.get("caption"))
        if isinstance(part, str) and part
    ]
    return "\n\n".join(parts)[:MAX_TEXT_CHARS]


def chat_sender_key_of(sender: object) -> str | None:
    """A sender's account-local identifier (a number or an address), lowercased, so that
    one participant is one sender."""
    if not isinstance(sender, str):
        return None
    return sender.strip().lower() or None


def parse_mailbox(document: object) -> SourceMailbox:
    if not isinstance(document, Mapping):
        raise SourceError
    account = document.get("account")
    messages = document.get("messages")
    if not isinstance(account, str) or not account or not isinstance(messages, list):
        raise SourceError
    items: dict[str, SourceItem] = {}
    skipped = 0
    for message in messages:
        item = parse_message(message)
        if item is None:
            skipped += 1
        elif item.upstream_id not in items:
            items[item.upstream_id] = item
    return SourceMailbox(account_key=account, items=list(items.values()), skipped=skipped)


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
        read_status=read_status_of(read),
        has_attachments=isinstance(attachments, list) and len(attachments) > 0,
        text=text_of(message),
    )


def text_of(message: Mapping[str, object]) -> str:
    """The subject and body, those of them that are strings, cut to the classifier's limit."""
    parts = [
        part for part in (message.get("subject"), message.get("body")) if isinstance(part, str)
    ]
    return "\n\n".join(parts)[:MAX_TEXT_CHARS]


def read_status_of(read: object) -> ReadStatus:
    """Only a real boolean names a status; anything else the source put there is unknown."""
    if isinstance(read, bool):
        return "read" if read else "unread"
    return "unknown"


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


FIXTURE_SOURCES: dict[str, type[FixtureSource]] = {
    "fixture_mail": FixtureMailSource,
    "fixture_messenger": FixtureMessengerSource,
    "imessage": IMessageSource,
}
# Kinds that send their credentials, as a bearer token, to the HTTP source they read.
BEARER_KINDS: frozenset[str] = frozenset({"imessage"})


def open_source(
    kind: str,
    location: str,
    credentials: Path | None,
    imessage_max_silence: timedelta | None = None,
) -> ItemSource:
    """The adapter for one account, once its credentials file has passed the mode check.

    `imessage_max_silence` is the setting of the iMessage source alone.
    Raises CredentialsError, which carries no detail, if the file cannot be used.
    """
    secret = read_credentials(credentials) if credentials is not None else None
    adapter = FIXTURE_SOURCES.get(kind)
    if adapter is None:
        raise SourceError
    is_url = urlsplit(location).scheme in ("http", "https")
    where = location if is_url else Path(location)
    bearer = secret if is_url and kind in BEARER_KINDS else None
    if adapter is IMessageSource:
        return IMessageSource(where, bearer, imessage_max_silence)
    return adapter(where, bearer)
