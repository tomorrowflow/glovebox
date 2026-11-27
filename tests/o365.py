"""A stand-in for Microsoft's side of an Office 365 mail account (ADR 0006, 0007).

One TLS listener on loopback plays the authority (`/{tenant}/...`: OIDC discovery, the token
endpoint and the device code endpoint, with refresh tokens that rotate as Microsoft's do) and
Microsoft Graph (`/v1.0/me/mailFolders/inbox/messages`, with `$filter`, `$top` and next links).
MSAL accepts only an https authority, so the stand-in has its own certificate; a test trusts
it through the environment variables the standard library and `requests` read, which the
gate honours without any setting of its own. Every request is recorded so that "the gate
sends nothing but reads" is a fact about requests. Faults are set one at a time and carry the
hostile corpus in every place an upstream could put text.
"""

import base64
import datetime as dt
import ipaddress
import json
import ssl
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from tests.hostile import CORPUS

TENANT = "tenant-0001"
CLIENT_ID = "client-0001"
OPERATOR = "alice@work.example.test"
GRAPH_PATH = "/v1.0/me/mailFolders/inbox/messages"
HOSTILE_ERROR = json.dumps(
    {"error": {"code": CORPUS["marker"], "message": "\n".join(CORPUS.values())}}
).encode()
# What Microsoft sends when the user or an admin has not consented, and when consent is gone.
TOKEN_FAULTS = {
    "consent_withdrawn": (400, "invalid_grant"),
    "interaction_required": (400, "interaction_required"),
    "consent_required": (400, "consent_required"),
    "unauthorized_client": (400, "unauthorized_client"),
    "unavailable": (503, "temporarily_unavailable"),
    "throttled": (429, "temporarily_unavailable"),
    "server_error": (500, "server_error"),
}
GRAPH_FAULTS = {
    "unauthorized": 401,
    "forbidden": 403,
    "throttled": 429,
    "unavailable": 503,
    "not_found": 404,
    "server_error": 500,
}


@dataclass(frozen=True)
class Seen:
    method: str
    path: str  # with the query
    authorization: str | None
    prefer: str | None


@dataclass
class Microsoft:
    """The state of the stand-in, which a test changes between calls."""

    url: str = ""
    messages: list[dict[str, Any]] = field(default_factory=list)
    # The permissions the authority puts in every token it issues.
    scope: str = "Mail.Read User.Read openid profile email"
    expires_in: int = 60  # short, so that every sync refreshes; a test sets 3600 to see the cache
    token_fault: str | None = None
    graph_fault: str | None = None
    delay: float = 0.0
    page_size: int | None = None  # a smaller page than the gate asked for
    requests: list[Seen] = field(default_factory=list)
    # What was posted to the token endpoint, and to the device code endpoint that starts a
    # sign-in: the permissions are asked for in the second.
    token_forms: list[dict[str, str]] = field(default_factory=list)
    device_forms: list[dict[str, str]] = field(default_factory=list)
    refresh_tokens: set[str] = field(default_factory=set)
    access_tokens: set[str] = field(default_factory=set)
    issued: int = 0

    @property
    def authority(self) -> str:
        return f"{self.url}/{TENANT}"

    @property
    def graph(self) -> str:
        return f"{self.url}/v1.0"

    def graph_requests(self) -> list[Seen]:
        return [seen for seen in self.requests if seen.path.startswith("/v1.0/")]

    def token_requests(self) -> list[Seen]:
        return [seen for seen in self.requests if seen.path.endswith("/oauth2/v2.0/token")]


def graph_message(
    message_id: str,
    sender: str,
    received: str,
    *,
    read: bool = False,
    attachments: bool = False,
    conversation: str | None = None,
    subject: str = "Quarterly figures SUBJ-5821",
    body: str = "Please see BODY-5821",
) -> dict[str, Any]:
    """A message as Graph returns it for the `$select` the gate sends."""
    return {
        "@odata.etag": f'W/"etag-{message_id}"',
        "id": message_id,
        "conversationId": conversation or f"conversation-{message_id}",
        "from": {"emailAddress": {"name": "SNAME-5821", "address": sender}},
        "receivedDateTime": received,
        "isRead": read,
        "hasAttachments": attachments,
        "subject": subject,
        "body": {"contentType": "text", "content": body},
    }


def certificate(directory: Path) -> Path:
    """A self-signed certificate for 127.0.0.1 and localhost, which doubles as the trust
    anchor a test hands the gate. The private key stays beside it in `key.pem`."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "o365 stand-in")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost")]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    (directory / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    path = directory / "standin.pem"
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return path


def trust_environment(certificate_path: Path) -> dict[str, str]:
    """The environment that makes a process trust the stand-in's certificate, and reach it
    directly whatever proxy the machine has."""
    return {
        "SSL_CERT_FILE": str(certificate_path),
        "REQUESTS_CA_BUNDLE": str(certificate_path),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }


def _segment(document: Mapping[str, object]) -> str:
    raw = json.dumps(document).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _id_token(client_id: str, issuer: str) -> str:
    now = int(dt.datetime.now(dt.UTC).timestamp())
    claims = {
        "iss": issuer,
        "aud": client_id,
        "sub": "subject-0001",
        "oid": "object-0001",
        "tid": TENANT,
        "preferred_username": OPERATOR,
        "iat": now,
        "nbf": now,
        "exp": now + 3600,
    }
    # MSAL reads the claims without checking the signature, as the gate does not need to.
    return f"{_segment({'alg': 'none'})}.{_segment(claims)}.c2lnbmF0dXJl"


def _form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length = int(handler.headers.get("Content-Length", 0))
    parsed = parse_qs(handler.rfile.read(length).decode(), keep_blank_values=True)
    return {name: values[0] for name, values in parsed.items()}


@contextmanager
def microsoft_serving(
    certificate_path: Path, messages: Sequence[dict[str, Any]] = ()
) -> Iterator[Microsoft]:
    stand_in = Microsoft(messages=list(messages))

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: bytes, extra: Mapping[str, str] | None = None) -> None:
            self.send_response(status, CORPUS["marker"] if status >= 400 else "OK")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # Text an upstream could hide in a header.
            self.send_header("X-Detail", CORPUS["injection"])
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, document: object) -> None:
            self._send(status, json.dumps(document).encode())

        def _handle(self) -> None:
            stand_in.requests.append(
                Seen(
                    self.command,
                    self.path,
                    self.headers.get("Authorization"),
                    self.headers.get("Prefer"),
                )
            )
            threading.Event().wait(stand_in.delay)
            parts = urlsplit(self.path)
            try:
                if parts.path.startswith("/v1.0/"):
                    self._graph(parts.path, parse_qs(parts.query, keep_blank_values=True))
                elif parts.path.startswith(f"/{TENANT}/"):
                    self._authority(parts.path)
                else:
                    self._send(404, HOSTILE_ERROR)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the gate gave up on this request

        def _authority(self, path: str) -> None:
            issuer = f"{stand_in.authority}/v2.0"
            if self.command == "GET" and path.endswith("/.well-known/openid-configuration"):
                prefix = f"{stand_in.authority}/oauth2/v2.0"
                self._json(
                    200,
                    {
                        "issuer": issuer,
                        "authorization_endpoint": f"{prefix}/authorize",
                        "token_endpoint": f"{prefix}/token",
                        "device_authorization_endpoint": f"{prefix}/devicecode",
                    },
                )
            elif self.command == "POST" and path.endswith("/oauth2/v2.0/devicecode"):
                stand_in.device_forms.append(_form(self))
                self._json(
                    200,
                    {
                        "user_code": "ABCD-EFGH",
                        "device_code": "device-code-0001",
                        "verification_uri": "https://microsoft.example.test/devicelogin",
                        "expires_in": 900,
                        "interval": 1,
                        "message": "To sign in, open the page and enter the code ABCD-EFGH.",
                    },
                )
            elif self.command == "POST" and path.endswith("/oauth2/v2.0/token"):
                self._token(_form(self), issuer)
            else:
                self._send(404, HOSTILE_ERROR)

        def _token(self, form: Mapping[str, str], issuer: str) -> None:
            stand_in.token_forms.append(dict(form))
            fault = stand_in.token_fault
            if fault in TOKEN_FAULTS:
                status, error = TOKEN_FAULTS[fault]
                headers = {"Retry-After": "120"} if status == 429 else {}
                body = {
                    "error": error,
                    "error_description": "\n".join(CORPUS.values()),
                    "error_codes": [65001],
                    "trace_id": CORPUS["marker"],
                    "correlation_id": CORPUS["injection"],
                }
                self._send(status, json.dumps(body).encode(), headers)
                return
            if fault == "garbage":
                self._send(200, "\n".join(CORPUS.values()).encode())
                return
            if fault == "wrong_shape":
                self._send(200, HOSTILE_ERROR)
                return
            if fault == "reset":
                self.close_connection = True
                return
            grant = form.get("grant_type")
            if grant == "refresh_token":
                presented = form.get("refresh_token", "")
                if presented not in stand_in.refresh_tokens:
                    self._json(400, {"error": "invalid_grant", "error_description": "gone"})
                    return
                stand_in.refresh_tokens.discard(presented)  # Microsoft rotates them
            elif grant != "urn:ietf:params:oauth:grant-type:device_code":
                self._json(400, {"error": "unsupported_grant_type"})
                return
            stand_in.issued += 1
            access, refresh = f"access-{stand_in.issued}", f"refresh-{stand_in.issued}"
            stand_in.access_tokens.add(access)
            stand_in.refresh_tokens.add(refresh)
            info = _segment({"uid": "object-0001", "utid": TENANT})
            self._json(
                200,
                {
                    "token_type": "Bearer",
                    "scope": stand_in.scope,
                    "expires_in": stand_in.expires_in,
                    "ext_expires_in": stand_in.expires_in,
                    "access_token": access,
                    "refresh_token": refresh,
                    "id_token": _id_token(form.get("client_id", CLIENT_ID), issuer),
                    "client_info": info,
                },
            )

        def _graph(self, path: str, query: Mapping[str, list[str]]) -> None:
            if self.command != "GET" or path != GRAPH_PATH:
                self._send(405, HOSTILE_ERROR)
                return
            bearer = self.headers.get("Authorization", "").removeprefix("Bearer ")
            fault = stand_in.graph_fault
            if bearer not in stand_in.access_tokens:
                self._send(401, HOSTILE_ERROR, {"WWW-Authenticate": f"Bearer {CORPUS['marker']}"})
            elif fault in GRAPH_FAULTS:
                headers = {"Retry-After": "120"} if fault == "throttled" else {}
                self._send(GRAPH_FAULTS[fault], HOSTILE_ERROR, headers)
            elif fault == "garbage":
                self._send(200, "\n".join(CORPUS.values()).encode())
            elif fault == "wrong_shape":
                self._send(200, HOSTILE_ERROR)
            elif fault == "not_a_list":
                self._json(200, {"value": CORPUS["injection"]})
            elif fault == "empty":
                self._send(200, b"")
            elif fault == "redirect":
                self.send_response(302)
                self.send_header("Location", CORPUS["injection"])
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif fault == "reset":
                self.close_connection = True
            elif fault == "truncated":
                self.send_response(200)
                self.send_header("Content-Length", "100000")
                self.end_headers()
                self.wfile.write(b'{"value": [')
            else:
                self._page(query)

        def _page(self, query: Mapping[str, list[str]]) -> None:
            floor = ""
            for clause in query.get("$filter", []):
                if clause.startswith("receivedDateTime ge "):
                    floor = clause.removeprefix("receivedDateTime ge ")
            matching = sorted(
                (m for m in stand_in.messages if str(m.get("receivedDateTime", "")) >= floor),
                key=lambda m: str(m.get("receivedDateTime", "")),
                reverse=True,
            )
            size = stand_in.page_size or int(query.get("$top", ["10"])[0])
            offset = int(query.get("$skiptoken", ["0"])[0])
            page: dict[str, Any] = {"value": matching[offset : offset + size]}
            if offset + size < len(matching):
                filter_ = quote(f"receivedDateTime ge {floor}")
                page["@odata.nextLink"] = (
                    f"{stand_in.graph}/me/mailFolders/inbox/messages"
                    f"?$skiptoken={offset + size}&$filter={filter_}&$top={size}"
                )
            if stand_in.graph_fault == "foreign_link":
                page["@odata.nextLink"] = "https://evil.example.test/steal"
            self._json(200, page)

        do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _handle

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certificate_path, certificate_path.with_name("key.pem"))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stand_in.url = f"https://127.0.0.1:{server.server_port}"
    try:
        yield stand_in
    finally:
        server.shutdown()
        server.server_close()
