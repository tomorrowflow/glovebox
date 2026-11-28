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
write, mailbox.org documents no read-only credential for it (docs/mailbox-org-mail.md), so the
protection is that the gate has no code path that sends anything else (ADR 0003).

The IMAP library is IMAPClient. This module raises its own errors, with no detail on purpose:
what a server or a message says stays out of exceptions, logs and responses.
"""

import imaplib
import ipaddress
import logging
import math
import socket
import ssl
import threading
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
# How much of each message is fetched: enough for the headers and the text the classifier
# is shown, not the attachments.
MAX_MESSAGE_BYTES = 64 * 1024
# The whole window is read, this many messages to a command, so no one response is unbounded.
BATCH_SIZE = 100
CONNECT_TIMEOUT_SECONDS = 10
# One read, from the name lookup to logout, is cut off after this long wherever the server stalls:
# a read that has not finished by then is an outage, never a smaller success.
DEFAULT_READ_DEADLINE = timedelta(seconds=120)
FETCH_ITEMS = ["FLAGS", "INTERNALDATE", "BODYSTRUCTURE", f"BODY.PEEK[]<0.{MAX_MESSAGE_BYTES}>"]
BODY_KEY = b"BODY[]<0>"
SEEN_LOWER = b"\\seen"  # system flags are case-insensitive (RFC 9051, 2.3.2): compare lowercased
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


class Budget:
    """The time one read may take, from the name lookup to the end of the logout.

    It is checked before every step, so retries and the end of the read count too. And it is
    enforced during one: the lookup and each connect attempt get only the time left, and every
    socket the read opens is watched from the moment it is connected and at the limit is shut
    down, which ends a TLS handshake, a greeting or a command that waits on a server that
    stalls or trickles bytes, where no socket timeout would. Use it as a context manager.
    """

    def __init__(self, limit: timedelta) -> None:
        self._end = time.monotonic() + limit.total_seconds()
        self._spent = threading.Event()
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self._timer: threading.Timer | None = None

    def __enter__(self) -> "Budget":
        self.start()
        return self

    def __exit__(self, *exception: object) -> None:
        self.stop()

    def start(self) -> None:
        """Start the clock; `stop` ends it when the read is over."""
        self._timer = threading.Timer(max(0.0, self._end - time.monotonic()), self._expire)
        self._timer.daemon = True
        self._timer.start()

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.cancel()

    def check(self) -> None:
        """Raises MailboxError once the limit has passed."""
        if self._spent.is_set() or time.monotonic() > self._end:
            raise MailboxError

    def spent(self) -> bool:
        return self._spent.is_set()

    def remaining(self, cap: float = math.inf) -> float:
        """The seconds left, at most `cap`, for a step that must not outlast the read.
        Raises MailboxError if there are none."""
        self.check()
        return min(cap, self._end - time.monotonic())

    def watch(self, connection: socket.socket) -> None:
        """Put a socket under the clock. Raises MailboxError if the limit has passed."""
        with self._lock:
            self.check()
            self._sockets.append(connection)

    def _expire(self) -> None:
        with self._lock:
            self._spent.set()
            watched = list(self._sockets)
        for connection in watched:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # already closed: the read has ended one way or the other


class WatchedConnection(imaplib.IMAP4):
    """The connection of one read, plain or TLS, set up under the read's clock: the name
    lookup and each connect attempt get the time left, and the socket is watched as soon as
    it is connected, before the TLS handshake and the greeting, which a server can stall or
    trickle like any other answer."""

    def __init__(
        self, host: str, port: int, context: ssl.SSLContext | None, budget: Budget
    ) -> None:
        self._context = context
        self._budget = budget
        super().__init__(host, port)

    def _create_socket(self, timeout: float | None = None) -> socket.socket:
        connection = self._reach_server()
        try:
            if self._context is not None:
                # A wrapped socket takes over the plain one's descriptor: watch the wrapped
                # one, and only then handshake.
                connection = self._context.wrap_socket(
                    connection, server_hostname=self.host, do_handshake_on_connect=False
                )
            self._budget.watch(connection)
            if isinstance(connection, ssl.SSLSocket):
                connection.do_handshake()
        except BaseException:
            connection.close()  # imaplib closes nothing of a connection that failed to open
            raise
        return connection

    def _reach_server(self) -> socket.socket:
        """A connected socket to the first address that answers, within the time left."""
        for family, kind, protocol, _, address in self._resolve():
            attempt = socket.socket(family, kind, protocol)
            try:
                attempt.settimeout(self._budget.remaining(CONNECT_TIMEOUT_SECONDS))
                attempt.connect(address)
            except OSError:
                attempt.close()  # the next address may answer
                continue
            except BaseException:
                attempt.close()
                raise
            return attempt
        raise OSError("no address answered")

    def _resolve(self) -> list[Any]:
        """The addresses of the host. The system resolver cannot be interrupted, so it runs
        on a thread that is left behind, harmlessly, if the time runs out first."""
        found: list[Any] = []

        def look() -> None:
            try:
                found.extend(socket.getaddrinfo(self.host, self.port, type=socket.SOCK_STREAM))
            except OSError:
                pass  # no addresses: handled as a host that does not answer

        lookup = threading.Thread(target=look, daemon=True)
        lookup.start()
        lookup.join(self._budget.remaining())
        self._budget.check()
        return found


class MailClient(IMAPClient):  # type: ignore[misc]  # the library has no type information
    """IMAPClient over a `WatchedConnection`, which is under the read's clock.

    The library builds its connection in `_create_IMAP4`, which is the one place to give it
    ours; the version is pinned exactly, and a test connects through this path.
    """

    def __init__(self, location: Location, context: ssl.SSLContext | None, budget: Budget) -> None:
        self._budget = budget
        super().__init__(
            location.host,
            location.port,
            ssl=location.tls,
            ssl_context=context,
            timeout=CONNECT_TIMEOUT_SECONDS,
        )

    def _create_IMAP4(self) -> WatchedConnection:
        # A TLS connection never goes without a context: that would be a plain one.
        context = (self.ssl_context or ssl.create_default_context()) if self.ssl else None
        return WatchedConnection(self.host, self.port, context, self._budget)


def read_inbox(
    location: Location,
    login: Login,
    since: datetime,
    limit: timedelta = DEFAULT_READ_DEADLINE,
) -> InboxRead:
    """The whole inbox window: messages received on or after `since` (UTC), and possibly up
    to a day before it.

    Raises MailboxBlocked if the server refuses the login and MailboxError for any other
    failure, including a read that takes longer than `limit`.
    """
    budget = Budget(limit)
    try:
        with budget:
            return _read(location, login, since, budget)
    except MailboxError:
        raise
    except LoginError:
        # A login cut off by the time limit is a stall, not a refusal.
        raise (MailboxError if budget.spent() else MailboxBlocked) from None
    except Exception:
        # The edge of the library and of a server: a refused or dropped connection, a
        # timeout, a TLS failure, a response the library cannot parse, a structure that is
        # nested too deeply. All are an unreadable source, and none may say anything more.
        raise MailboxError from None


def _read(location: Location, login: Login, since: datetime, budget: Budget) -> InboxRead:
    budget.check()
    context = ssl.create_default_context() if location.tls else None
    with MailClient(location, context, budget) as client:  # logs out on the way out
        client.normalise_times = False  # keep the offset the server gave
        client.login(login.username, login.password.reveal())
        folder: dict[bytes, Any] = client.select_folder(FOLDER, readonly=True)
        validity = folder.get(b"UIDVALIDITY")
        if not isinstance(validity, int):
            raise MailboxError
        # SINCE compares dates only, in the server's zone: start a day early, and let the
        # gate drop what falls outside the window.
        wanted: list[int] = sorted(client.search(["SINCE", since.date() - timedelta(days=1)]))
        messages: list[InboxMessage] = []
        skipped = 0
        for start in range(0, len(wanted), BATCH_SIZE):
            batch, unreadable = _fetch(client, wanted[start : start + BATCH_SIZE], validity, budget)
            messages.extend(batch)
            skipped += unreadable
    # A read that ran out of time on its last command, or on the logout whose failure the
    # client swallows, is no success either.
    budget.check()
    return InboxRead(messages, skipped)


def _fetch(
    client: IMAPClient, uids: Sequence[int], validity: int, budget: Budget
) -> tuple[list[InboxMessage], int]:
    """A batch of messages and the number that could not be understood.

    A response the library cannot parse is retried a message at a time, so that one
    malformed message costs one skip and not the batch.
    """
    budget.check()
    try:
        response = client.fetch(uids, FETCH_ITEMS)
    except ProtocolError:
        budget.check()  # a cut-off connection can look like a malformed response
        if len(uids) == 1:
            return [], 1
        found: list[InboxMessage] = []
        unreadable = 0
        for uid in uids:
            one, skipped = _fetch(client, [uid], validity, budget)
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
            read=any(flag.lower() == SEEN_LOWER for flag in data[b"FLAGS"]),
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
            return _normalised_id(identifiers[0])
    return ""


def _normalised_id(identifier: str) -> str:
    """A message ID spelled one way: its domain lowercased, its local part as it is, since
    only the domain is case-insensitive (RFC 5322, 3.6.4)."""
    local, separator, domain = identifier.rpartition("@")
    return f"{local}@{domain.lower()}" if separator else identifier


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
