"""An IMAP server stand-in for the mailbox.org mail source (ADR 0003, 0006).

It speaks the part of IMAP a mail reader uses, over plain TCP on loopback, from a list of
messages a test gives it. It records every command it receives, whatever it is, so that
"the gate sends no command that changes the mailbox" is a fact about commands. It is also
honest about state: a `SELECT` (read-write) followed by a fetch that does not peek marks the
message `\\Seen`, a `STORE` changes flags, and `EXAMINE` refuses both, as a real server
does. A test that reads the flags afterwards therefore sees a mutation the adapter made.
Faults are set one at a time with `fault`.
"""

import re
import socketserver
import ssl
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email import policy
from email.message import EmailMessage, Message
from email.utils import formataddr
from ipaddress import ip_address
from pathlib import Path
from typing import Any, cast

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from tests.hostile import CORPUS

USER = "operator@mailbox.test"
PASSWORD = "imap-secret-0123456789abcdef"
FOLDER = "INBOX"
UIDVALIDITY = 7001
STALL_SECONDS = 60
# Free text on every status line of the server: a client must never pass it on.
NOISE = CORPUS["injection"].encode()
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# The commands a read-only reader may send. Anything else is a failure of the adapter.
READ_COMMANDS = frozenset(
    {"CAPABILITY", "LOGIN", "EXAMINE", "UID SEARCH", "UID FETCH", "NOOP", "ID", "LOGOUT"}
)
# What a fetch item must look like to leave `\Seen` alone: BODY.PEEK[...] and metadata only.
PEEKING_ITEM = re.compile(
    r"^(UID|FLAGS|INTERNALDATE|RFC822\.SIZE|BODYSTRUCTURE|BODY\.PEEK\[[^\]]*\](<\d+\.\d+>)?)$",
    re.IGNORECASE,
)


@dataclass
class Mail:
    """One message in the stand-in's mailbox: its bytes, as the server would hold them."""

    uid: int
    raw: bytes
    structure: bytes
    received: datetime
    flags: set[str] = field(default_factory=set)
    internaldate: bytes | None = None  # what the server says instead of `received`


def quoted(text: bytes) -> bytes:
    """An IMAP string: quoted if it can be, a literal if it holds anything awkward."""
    if re.fullmatch(rb"[ -~]*", text) and b'"' not in text and b"\\" not in text:
        return b'"' + text + b'"'
    return b"{%d}\r\n" % len(text) + text


def leaf_structure(part: Message) -> bytes:
    """The BODYSTRUCTURE of one part that holds no other parts (the extensible form)."""
    params = part.get_params(failobj=[])[1:]
    flat = b" ".join(quoted(str(item).encode()) for pair in params for item in pair)
    parameters = b"(" + flat + b")" if flat else b"NIL"
    maintype, subtype = part.get_content_maintype(), part.get_content_subtype()
    payload = part.get_payload(decode=True) or b""
    disposition = part.get_content_disposition()
    filename = part.get_filename()
    if disposition is None:
        extension = b"NIL"
    else:
        named = b'("FILENAME" ' + quoted(filename.encode()) + b")" if filename else b"NIL"
        extension = b"(" + quoted(disposition.upper().encode()) + b" " + named + b")"
    lines = b" 1" if maintype == "text" else b""
    return (
        b"("
        + quoted(maintype.upper().encode())
        + b" "
        + quoted(subtype.upper().encode())
        + b" "
        + parameters
        + b' NIL NIL "7BIT" '
        + str(len(payload)).encode()
        + lines
        + b" NIL "
        + extension
        + b" NIL)"
    )


def bodystructure(message: Message) -> bytes:
    """The BODYSTRUCTURE of a message: a leaf, or a multipart around its children."""
    if not message.is_multipart():
        return leaf_structure(message)
    children = b"".join(
        bodystructure(child) for child in cast(list[Message], message.get_payload())
    )
    subtype = quoted(message.get_content_subtype().upper().encode())
    return b"(" + children + b" " + subtype + b")"


def mail(
    uid: int,
    sender: str,
    received: str,
    *,
    subject: str = "",
    body: str = "",
    read: bool = False,
    attachments: Sequence[str] = (),
    message_id: str | None = None,
    references: str | None = None,
    in_reply_to: str | None = None,
    html: str | None = None,
) -> Mail:
    """A well-formed message. `attachments` are filenames; `received` is an ISO time; `html`
    makes the body an HTML one in place of `body`."""
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = sender
    message["To"] = "operator@mailbox.test"
    message["Subject"] = subject
    message["Date"] = "Mon, 05 Jan 2026 18:00:00 +0000"
    message["Message-ID"] = message_id or f"<m{uid}@example.test>"
    if references:
        message["References"] = references
    if in_reply_to:
        message["In-Reply-To"] = in_reply_to
    if html is None:
        message.set_content(body or "(no text)")
    else:
        message.set_content(html, subtype="html")
    for name in attachments:
        message.add_attachment(b"%PDF-1.4 fictional", "application", "pdf", filename=name)
    return Mail(
        uid=uid,
        raw=message.as_bytes(),
        structure=bodystructure(message),
        received=datetime.fromisoformat(received).astimezone(UTC),
        flags={"\\Seen"} if read else set(),
    )


def hostile_inbox() -> list[Mail]:
    """Mail with a corpus string in every place a sender controls, two messages per string.

    The first is written byte by byte, so that the string sits unescaped in the From, To, Cc,
    Subject, Message-ID, References, Reply-To and an extra header, in the parameters of the
    Content-Type, in the text part, and in the name, parameters, disposition filename and
    content of an attachment, and a newline in it ends or injects headers. The second is
    well-formed, with the string made into one line where a header needs that. Every message
    also carries the corpus marker as a server-set flag.
    """
    mails = []
    for index, text in enumerate(CORPUS.values()):
        uid = 2 * index + 1
        boundary = f"gbx-boundary-{uid}"
        raw = (
            f'From: "{text}" <sender{uid}@evil.test>\r\n'
            f"To: {text}\r\nCc: {text}\r\nSubject: {text}\r\n"
            f"Message-ID: <{text}@evil.test>\r\nReferences: <{text}@evil.test>\r\n"
            f"Reply-To: {text}\r\nX-Evil: {text}\r\n"
            f'Content-Type: multipart/mixed; boundary="{boundary}"; x="{text}"\r\n\r\n'
            f"--{boundary}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{text}\r\n"
            f'--{boundary}\r\nContent-Type: application/octet-stream; name="{text}"; x="{text}"\r\n'
            f'Content-Disposition: attachment; filename="{text}"\r\n\r\n{text}\r\n'
            f"--{boundary}--\r\n"
        ).encode()
        data = text.encode()
        structure = (
            b'(("TEXT" "PLAIN" ("CHARSET" "utf-8") NIL NIL "7BIT" %d 1)' % len(data)
            + b'("APPLICATION" "OCTET-STREAM" ("NAME" %s "X" %s) NIL NIL "BASE64" %d NIL'
            % (quoted(data), quoted(data), len(data))
            + b' ("ATTACHMENT" ("FILENAME" %s)) NIL) "MIXED")' % quoted(data)
        )
        received = datetime(2026, 1, 8, 8, index, 37, tzinfo=UTC)
        mails.append(Mail(uid, raw, structure, received, {CORPUS["marker"]}))
        line = " ".join(text.split())
        clean = mail(
            uid + 1,
            formataddr((line, f"sender{uid + 1}@evil.test")),
            received.isoformat(),
            subject=line,
            body=text,
            attachments=[line],
            message_id=f"<clean-{uid + 1}@evil.test>",
        )
        clean.flags.add(CORPUS["marker"])
        mails.append(clean)
    return mails


def tokens(text: str) -> list[str]:
    """The words of a command's arguments, quoted strings unquoted."""
    return [
        (quoted_word[1:-1].replace('\\"', '"').replace("\\\\", "\\") if quoted_word else word)
        for quoted_word, word in re.findall(r'("(?:[^"\\]|\\.)*")|(\S+)', text)
    ]


def uid_set(text: str, mails: Sequence[Mail]) -> list[Mail]:
    wanted: set[int] = set()
    top = max((m.uid for m in mails), default=0)
    for part in text.split(","):
        low, _, high = part.partition(":")
        start = top if low == "*" else int(low)
        end = (top if high == "*" else int(high)) if high else start
        wanted.update(range(min(start, end), max(start, end) + 1))
    return [m for m in mails if m.uid in wanted]


def fetch_items(text: str) -> list[str]:
    """The data items of a FETCH, outside any brackets: `(UID BODY.PEEK[HEADER.FIELDS (A B)])`."""
    text = text.strip()
    if text.startswith("(") and text.endswith(")"):
        text = text[1:-1]
    items, depth, current = [], 0, ""
    for char in text:
        if char in "[(":
            depth += 1
        elif char in "])":
            depth -= 1
        if char == " " and depth == 0:
            items.append(current)
            current = ""
        else:
            current += char
    return [*items, current] if current else items


def render_item(item: str, message: Mail, writable: bool) -> bytes:
    name = item.upper()
    if name == "UID":
        return b"UID %d" % message.uid
    if name == "FLAGS":
        return b"FLAGS (" + " ".join(sorted(message.flags)).encode() + b")"
    if name == "INTERNALDATE":
        if message.internaldate is not None:
            return b"INTERNALDATE " + message.internaldate
        when = message.received
        stamp = f"{when.day:02d}-{MONTHS[when.month - 1]}-{when.year} {when:%H:%M:%S} +0000"
        return b'INTERNALDATE "' + stamp.encode() + b'"'
    if name == "RFC822.SIZE":
        return b"RFC822.SIZE %d" % len(message.raw)
    if name == "BODYSTRUCTURE":
        return b"BODYSTRUCTURE " + message.structure
    found = re.fullmatch(r"BODY(\.PEEK)?\[([^\]]*)\](?:<(\d+)\.(\d+)>)?", item, re.IGNORECASE)
    if found is None:
        raise ValueError(item)
    peek, section, start, length = found.groups()
    if section.strip():
        raise ValueError(item)  # only the whole message is served
    data = message.raw
    key = f"BODY[{section}]"
    if start is not None:
        data = data[int(start) : int(start) + int(length)]
        key += f"<{start}>"
    if writable and not peek:
        message.flags.add("\\Seen")
    return key.encode() + b" {%d}\r\n" % len(data) + data


class ImapStandIn:
    """The server's state: its messages, its faults and what it was asked."""

    def __init__(self, mails: Sequence[Mail], user: str = USER, password: str = PASSWORD) -> None:
        self.mails = list(mails)
        self.user = user
        self.password = password
        self.fault: str | None = None
        self.port = 0
        self.scheme = "imap"
        self.commands: list[str] = []
        self.fetches: list[str] = []  # the data items of every fetch, as sent
        self.connections = 0
        self.fetch_delay = 0.0  # seconds every fetch takes to be answered
        self.released = threading.Event()  # set when the test is over: ends stalls at once
        self._lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"{self.scheme}://127.0.0.1:{self.port}"

    def flags(self) -> dict[int, set[str]]:
        return {m.uid: set(m.flags) for m in self.mails}

    def record(self, command: str) -> None:
        with self._lock:
            self.commands.append(command)


class Handler(socketserver.StreamRequestHandler):
    server: Any

    def send(self, *lines: bytes) -> None:
        self.wfile.write(b"".join(line + b"\r\n" for line in lines))
        self.wfile.flush()

    def handle(self) -> None:
        standin: ImapStandIn = self.server.standin
        standin.connections += 1
        if standin.fault == "down":
            return
        if standin.fault == "greeting_stall":
            # Accepts the connection and says nothing: neither a greeting nor, to a client
            # that speaks TLS first, a handshake.
            standin.released.wait(STALL_SECONDS)
            return
        if standin.fault == "greeting_trickle":
            # A greeting that never ends and never goes quiet: no socket timeout can fire.
            try:
                while not standin.released.wait(0.2):
                    self.wfile.write(b"*")
                    self.wfile.flush()
            except OSError:
                pass  # the client gave up on the connection
            return
        if standin.fault == "bye":
            self.send(b"* BYE " + CORPUS["injection"].encode())
            return
        self.send(b"* OK [CAPABILITY IMAP4rev1 UIDPLUS] ready " + NOISE)
        authenticated, selected, writable = False, False, False
        while True:
            try:
                raw = self.rfile.readline(65536)
            except OSError:
                return
            if not raw:
                return
            tag, _, rest = raw.decode("utf-8", "replace").rstrip("\r\n").partition(" ")
            verb, _, arguments = rest.partition(" ")
            verb = verb.upper()
            if verb == "UID":
                sub, _, arguments = arguments.partition(" ")
                verb = f"UID {sub.upper()}"
            standin.record(verb)
            if verb == "CAPABILITY":
                self.send(b"* CAPABILITY IMAP4rev1 UIDPLUS", b"%s OK done " % tag.encode() + NOISE)
            elif verb == "NOOP":
                self.send(b"%s OK done " % tag.encode() + NOISE)
            elif verb == "ID":
                self.send(b"* ID NIL", b"%s OK done " % tag.encode() + NOISE)
            elif verb == "LOGOUT":
                if standin.fault == "logout_stall":
                    standin.released.wait(STALL_SECONDS)
                    return
                self.send(b"* BYE bye", b"%s OK done " % tag.encode() + NOISE)
                return
            elif verb == "LOGIN":
                words = tokens(arguments)
                if standin.fault == "auth" or words != [standin.user, standin.password]:
                    detail = CORPUS["injection"].encode()
                    self.send(b"%s NO [AUTHENTICATIONFAILED] " % tag.encode() + detail)
                else:
                    authenticated = True
                    self.send(b"%s OK logged in " % tag.encode() + NOISE)
            elif not authenticated:
                self.send(b"%s BAD login first" % tag.encode())
            elif verb in ("SELECT", "EXAMINE"):
                folder = tokens(arguments)[:1]
                if standin.fault == "select_no" or folder != [FOLDER]:
                    self.send(b"%s NO %s" % (tag.encode(), CORPUS["role_tag"].encode()))
                    continue
                selected, writable = True, verb == "SELECT"
                mode = b"READ-WRITE" if writable else b"READ-ONLY"
                self.send(
                    b"* %d EXISTS" % len(standin.mails),
                    b"* FLAGS (\\Seen \\Answered \\Flagged \\Deleted \\Draft)",
                    b"* OK [UIDVALIDITY %d] ok" % UIDVALIDITY,
                    b"* OK [UIDNEXT %d] ok" % (max((m.uid for m in standin.mails), default=0) + 1),
                    b"%s OK [%s] %s done " % (tag.encode(), mode, verb.encode()) + NOISE,
                )
            elif not selected:
                self.send(b"%s BAD select first" % tag.encode())
            elif verb == "UID SEARCH":
                self.search(tag, arguments)
            elif verb == "UID FETCH":
                if self.fetch(tag, arguments, writable):
                    return
            elif verb == "UID STORE" and writable:
                number, _, change = arguments.partition(" ")
                for message in uid_set(number, standin.mails):
                    flags = set(re.findall(r"\\\w+", change))
                    sign = change.split()[0][0] if change.split() else "+"
                    message.flags = message.flags - flags if sign == "-" else message.flags | flags
                self.send(b"%s OK stored " % tag.encode() + NOISE)
            else:
                self.send(b"%s NO %s" % (tag.encode(), CORPUS["tool_call"].encode()))

    def search(self, tag: str, arguments: str) -> None:
        standin: ImapStandIn = self.server.standin
        chosen = standin.mails
        since = re.search(r"SINCE (\d{1,2})-(\w{3})-(\d{4})", arguments, re.IGNORECASE)
        if since is not None:
            day, month, year = since.groups()
            limit = datetime(int(year), MONTHS.index(month.title()) + 1, int(day), tzinfo=UTC)
            chosen = [m for m in chosen if m.received >= limit]
        numbers = b" ".join(b"%d" % m.uid for m in chosen)
        self.send(b"* SEARCH " + numbers, b"%s OK done " % tag.encode() + NOISE)

    def fetch(self, tag: str, arguments: str, writable: bool) -> bool:
        """Answer a fetch; True if the connection is to be dropped."""
        standin: ImapStandIn = self.server.standin
        numbers, _, wanted = arguments.partition(" ")
        items = fetch_items(wanted)
        standin.fetches.append(" ".join(items))
        if standin.fault == "fetch_drop":
            return True
        if standin.fault == "stall":
            standin.released.wait(STALL_SECONDS)
            return True
        if standin.fault == "trickle":
            # A response that never ends and never goes quiet: no socket timeout can fire.
            try:
                self.wfile.write(b"* 1 FETCH (UID 1 BODY[]<0> {1000000}\r\n")
                while not standin.released.wait(0.2):
                    self.wfile.write(b"x")
                    self.wfile.flush()
            except OSError:
                pass  # the client gave up on the connection
            return True
        standin.released.wait(standin.fetch_delay)
        if standin.fault == "fetch_no":
            self.send(b"%s NO %s" % (tag.encode(), CORPUS["tool_call"].encode()))
            return False
        if standin.fault == "garbage":
            self.send(*(f"* {value}".encode() for value in CORPUS.values()))
            self.send(b"%s OK done " % tag.encode() + NOISE)
            return False
        chosen = uid_set(numbers, standin.mails)
        for message in chosen:
            sequence = standin.mails.index(message) + 1
            body = b" ".join(render_item(item, message, writable) for item in items)
            self.wfile.write(b"* %d FETCH (UID %d " % (sequence, message.uid) + body + b")\r\n")
        self.wfile.flush()
        self.send(b"%s OK done " % tag.encode() + NOISE)
        return False


def self_signed_certificate(directory: Path) -> tuple[Path, Path]:
    """A certificate for 127.0.0.1 and its private key, as PEM files in `directory`."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ip_address("127.0.0.1"))]), False
        )
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = directory / "standin.pem", directory / "standin.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@contextmanager
def imap_serving(
    mails: Sequence[Mail], certificate: tuple[Path, Path] | None = None, **credentials: str
) -> Iterator[ImapStandIn]:
    """The stand-in on a free loopback port; with a `certificate` (certificate file, key file)
    it speaks TLS from the first byte, as an `imaps` server does."""
    standin = ImapStandIn(mails, **credentials)
    context = None
    if certificate is not None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(*certificate)
        standin.scheme = "imaps"

    class Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True

        def get_request(self) -> tuple[Any, Any]:
            connection, address = super().get_request()
            if context is not None:
                # The handshake happens in the handler's thread, at its first read or write.
                connection = context.wrap_socket(
                    connection, server_side=True, do_handshake_on_connect=False
                )
            return connection, address

    server = Server(("127.0.0.1", 0), Handler)
    cast(Any, server).standin = standin
    standin.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield standin
    finally:
        standin.released.set()
        server.shutdown()
        server.server_close()
