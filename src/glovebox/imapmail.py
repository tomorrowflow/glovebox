"""Read mailbox.org mail over IMAP, without changing the mailbox.

The gate logs in as the operator, opens the inbox with `EXAMINE` (read-only) and fetches the
messages of the retrieval window with `BODY.PEEK`, which does not set `\\Seen`. From each it
takes the sender, the thread's root message ID, the received time the server recorded
(`INTERNALDATE`, not the sender-written `Date`), the `\\Seen` flag, whether the structure
holds an attachment and the subject and body text, which goes to the quarantined classifier
and nowhere else. Recipients, names, filenames, MIME parameters and every other header are
never read out of the message (ADR 0004). Nothing is copied: the mail is read where it is.

Only four IMAP commands past login are ever sent: `EXAMINE`, `UID SEARCH`, `UID FETCH` with
peeking items, and `LOGOUT`. That is gateway-enforced read-only: an IMAP login can also
write, no read-only IMAP credential is known to exist at mailbox.org (docs/mailbox-org-mail.md), so
the protection is that the gate has no code path that sends anything else (ADR 0003).

The IMAP library is IMAPClient. This module raises its own errors, with no detail on purpose:
what a server or a message says stays out of exceptions, logs and responses.
"""

import ipaddress
import logging
import ssl
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import message_from_bytes, policy
from email.message import EmailMessage
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

from imapclient import IMAPClient
from imapclient.exceptions import LoginError, ProtocolError

from glovebox.credentials import Secret

# Everything IMAPClient logs would carry what the server said, and so would its imaplib
# traffic log; none of it may reach the gate's log.
logging.getLogger("imapclient").setLevel(logging.CRITICAL)

FOLDER = "INBOX"
DEFAULT_TLS_PORT = 993
PLAIN_PORT = 143
# The most messages one read returns. A window holding more is cut to the newest, and the
# rest is reported as skipped, so that the account is `incomplete`, not `current`.
MAX_MESSAGES = 2000
# How much of each message is fetched: enough for the headers and the text the classifier
# is shown, not the attachments.
MAX_MESSAGE_BYTES = 64 * 1024
BATCH_SIZE = 100
CONNECT_TIMEOUT_SECONDS = 10
# One read, connection to logout, is cut off after this long wherever the server stalls.
READ_DEADLINE_SECONDS = 60
FETCH_ITEMS = ["FLAGS", "INTERNALDATE", "BODYSTRUCTURE", f"BODY.PEEK[]<0.{MAX_MESSAGE_BYTES}>"]
BODY_KEY = b"BODY[]<0>"
SEEN = b"\\Seen"
# Parts that are no attachment: a signature is part of a signed message, not a file.
SIGNATURE_SUBTYPES = frozenset({b"PGP-SIGNATURE", b"PKCS7-SIGNATURE", b"X-PKCS7-SIGNATURE"})
ATTACHMENT = b"ATTACHMENT"
# Where a leaf part's Content-Disposition sits in its BODYSTRUCTURE (RFC 3501, 7.4.2): after
# the size, the line count of a text part, and the envelope, structure and line count of an
# embedded message.
DISPOSITION_OF_TEXT, DISPOSITION_OF_MESSAGE, DISPOSITION_OF_OTHER = 9, 11, 8


class MailboxError(Exception):
    """The mailbox could not be read or understood. It carries no detail."""


class MailboxBlocked(MailboxError):
    """The server refused the login, or the login or the address is unusable."""


@dataclass(frozen=True)
class Location:
    host: str
    port: int
    tls: bool


@dataclass(frozen=True)
class Login:
    username: str
    password: Secret = field(repr=False)


@dataclass(frozen=True)
class InboxMessage:
    key: str  # unique for the message in this mailbox, stable across reads
    sender: str  # the raw From value
    thread: str  # the message ID of the thread's root, as far as the headers say
    received: datetime  # UTC
    read: bool
    has_attachments: bool
    text: str = field(repr=False)


@dataclass(frozen=True)
class InboxRead:
    messages: list[InboxMessage]
    skipped: int = 0  # messages the server held that could not be understood, or did not fit


def is_loopback_host(host: str) -> bool:
    """Whether a host name or address is this host: `localhost`, or a loopback address."""
    try:
        return host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def location_problem(source: str) -> str | None:
    """Why an IMAP location may not be used, in fixed text, or None.

    The login goes only over TLS (`imaps://`) or to this host (`imap://` on loopback, which
    is how the test tier's stand-in is reached), never in clear across a network.
    """
    parts = urlsplit(source)
    try:
        port = parts.port
    except ValueError:
        port = -1
    if parts.scheme not in ("imap", "imaps") or not parts.hostname or port == -1:
        return "an IMAP source is imaps://HOST or imaps://HOST:PORT"
    if parts.path not in ("", "/"):
        return "an IMAP source names a server only; the inbox is always the one that is read"
    if parts.scheme == "imap" and not is_loopback_host(parts.hostname):
        return "the login is sent only over TLS or to this host: use an imaps:// source"
    return None


def location_of(source: str) -> Location:
    """The server an IMAP source names. Raises MailboxBlocked if it may not be used."""
    if location_problem(source) is not None:
        raise MailboxBlocked
    parts = urlsplit(source)
    tls = parts.scheme == "imaps"
    assert parts.hostname is not None
    return Location(parts.hostname, parts.port or (DEFAULT_TLS_PORT if tls else PLAIN_PORT), tls)


def login_of(secret: Secret) -> Login | None:
    """The login in a credentials file: the user name on the first line, the password on the
    second. None if the file is not that."""
    lines = secret.reveal().splitlines()
    if len(lines) != 2 or not all(line.strip() for line in lines):
        return None
    return Login(lines[0].strip(), Secret(lines[1]))


def read_inbox(location: Location, login: Login, since: datetime) -> InboxRead:
    """The inbox's messages received on or after `since` (UTC), and possibly up to a day before it.

    Raises MailboxBlocked if the server refuses the login and MailboxError for any other
    failure.
    """
    deadline = time.monotonic() + READ_DEADLINE_SECONDS
    try:
        return _read(location, login, since, deadline)
    except MailboxError:
        raise
    except LoginError:
        raise MailboxBlocked from None
    except Exception:
        # The edge of the library and of a server: a refused or dropped connection, a
        # timeout, a TLS failure, a response the library cannot parse, a structure that is
        # nested too deeply. All are an unreadable source, and none may say anything more.
        raise MailboxError from None


def _read(location: Location, login: Login, since: datetime, deadline: float) -> InboxRead:
    context = ssl.create_default_context() if location.tls else None
    with IMAPClient(
        location.host,
        location.port,
        ssl=location.tls,
        ssl_context=context,
        timeout=CONNECT_TIMEOUT_SECONDS,
    ) as client:
        client.normalise_times = False  # keep the offset the server gave
        client.login(login.username, login.password.reveal())
        folder: dict[bytes, Any] = client.select_folder(FOLDER, readonly=True)
        validity = folder.get(b"UIDVALIDITY")
        if not isinstance(validity, int):
            raise MailboxError
        # SINCE compares dates only, in the server's zone: start a day early, and let the
        # gate drop what falls outside the window.
        found: list[int] = sorted(client.search(["SINCE", since.date() - timedelta(days=1)]))
        wanted = found[-MAX_MESSAGES:]
        skipped = len(found) - len(wanted)
        messages: list[InboxMessage] = []
        for start in range(0, len(wanted), BATCH_SIZE):
            if time.monotonic() > deadline:
                raise MailboxError
            batch, unreadable = _fetch(client, wanted[start : start + BATCH_SIZE], validity)
            messages.extend(batch)
            skipped += unreadable
    return InboxRead(messages, skipped)


def _fetch(
    client: IMAPClient, uids: Sequence[int], validity: int
) -> tuple[list[InboxMessage], int]:
    """A batch of messages and the number that could not be understood.

    A response the library cannot parse is retried a message at a time, so that one
    malformed message costs one skip and not the batch.
    """
    try:
        response = client.fetch(uids, FETCH_ITEMS)
    except ProtocolError:
        if len(uids) == 1:
            return [], 1
        found: list[InboxMessage] = []
        unreadable = 0
        for uid in uids:
            one, skipped = _fetch(client, [uid], validity)
            found.extend(one)
            unreadable += skipped
        return found, unreadable
    messages: list[InboxMessage] = []
    unreadable = len(uids) - len(response)  # asked for, and not answered
    for uid, data in response.items():
        message = _message(f"{validity}:{uid}", data)
        if message is None:
            unreadable += 1
        else:
            messages.append(message)
    return messages, unreadable


def _message(key: str, data: dict[bytes, Any]) -> InboxMessage | None:
    """One fetched message as an item, or None if it cannot be understood."""
    try:
        received = data[b"INTERNALDATE"]
        raw = data[BODY_KEY]
        if received.tzinfo is None or not isinstance(raw, bytes):
            return None
        parsed = message_from_bytes(raw, policy=policy.default)
        assert isinstance(parsed, EmailMessage)
        sender = str(parsed["From"] or "").strip()
        if not sender:
            return None
        return InboxMessage(
            key=key,
            sender=sender,
            thread=_thread_of(parsed) or key,
            received=received.astimezone(UTC),
            read=SEEN in data[b"FLAGS"],
            has_attachments=has_attachment(data[b"BODYSTRUCTURE"]),
            text=_text_of(parsed),
        )
    except Exception:
        # Whatever is wrong with this one message is its sender's doing: it is skipped.
        return None


def _thread_of(message: EmailMessage) -> str:
    """The ID of the message that started the thread: the first of its References, else the
    one it replies to, else its own."""
    for header in ("References", "In-Reply-To", "Message-ID"):
        identifiers = str(message[header] or "").split()
        if identifiers:
            return identifiers[0].lower()
    return ""


def has_attachment(structure: Any, related: bool = False) -> bool:
    """Whether a BODYSTRUCTURE holds a part meant as a file.

    A part is one when its disposition says `attachment`, or when it has none and is neither
    text, nor a signature, nor part of a `multipart/related` (the images of an HTML body).
    Only the disposition's type is read, never the name.
    """
    if isinstance(structure[0], list):  # a multipart: its parts, then its subtype
        inside = related or structure[1].upper() == b"RELATED"
        return any(has_attachment(part, inside) for part in structure[0])
    kind, subtype = structure[0].upper(), structure[1].upper()
    index = {b"TEXT": DISPOSITION_OF_TEXT, b"MESSAGE": DISPOSITION_OF_MESSAGE}.get(
        kind, DISPOSITION_OF_OTHER
    )
    disposition = structure[index] if len(structure) > index else None
    if disposition is not None:
        return bool(disposition[0].upper() == ATTACHMENT)
    return kind != b"TEXT" and subtype not in SIGNATURE_SUBTYPES and not related


class _Text(HTMLParser):
    """The visible text of an HTML body."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._hidden:
            self._hidden -= 1

    def handle_data(self, data: str) -> None:
        if not self._hidden:
            self.parts.append(data)


def _text_of(message: EmailMessage) -> str:
    """The subject and the readable body."""
    subject = str(message["Subject"] or "")
    body = message.get_body(preferencelist=("plain", "html"))
    content = body.get_content() if body is not None else ""
    if body is not None and body.get_content_subtype() == "html":
        extractor = _Text()
        extractor.feed(content)
        content = " ".join(extractor.parts)
    return f"{subject}\n\n{content}"
