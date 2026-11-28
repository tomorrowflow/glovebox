"""A scripted stand-in for Microsoft's sign-in service and Microsoft Graph, over TLS.

`msal` talks only to https authorities, so the stand-in serves HTTPS with a certificate the
tests make for 127.0.0.1 and hand to the gate through `SSL_CERT_FILE` and
`REQUESTS_CA_BUNDLE`. It answers the OpenID configuration, the device code and token
endpoints, and `GET /v1.0/me/calendarView`, and records every request so that a test can say
what the gate asked for and what it never sent. It is a test double: it says nothing about
how the real services behave (ADR 0003).
"""

import base64
import datetime as dt
import ipaddress
import json
import ssl
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

TENANT = "tenant-1"
CLIENT_ID = "client-1"
DEVICE_GRANT = "urn:ietf:params:oauth:grant-type:device_code"
OIDC_SCOPES = {"openid", "profile", "offline_access"}


def make_certificate(directory: Path) -> tuple[Path, Path]:
    """A self-signed certificate for 127.0.0.1 and its key, as PEM files."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "o365 stand-in")])
    now = dt.datetime.now(dt.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certificate_file, key_file = directory / "standin.pem", directory / "standin-key.pem"
    certificate_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_file, key_file


def _segment(value: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")


def id_token() -> str:
    """An unsigned ID token: msal reads its claims to tell the signed-in account."""
    claims = {"aud": CLIENT_ID, "oid": "user-1", "tid": TENANT, "preferred_username": "a@x.test"}
    return ".".join([_segment({"alg": "none"}), _segment(claims), "signature"])


@dataclass
class Recorded:
    method: str
    path: str
    query: dict[str, list[str]]
    headers: dict[str, str]
    form: dict[str, list[str]]


@dataclass
class Script:
    """What the stand-in answers; a test changes it between calls."""

    events: list[object] = field(default_factory=list)
    page_size: int = 100
    graph_status: int = 200
    graph_body: bytes | None = None  # answers every Graph request, whatever its status
    token_status: int = 200
    token_body: bytes | None = None  # answers every token request, whatever its status
    granted_scope: str = "Calendars.Read"
    next_link_base: str | None = None  # where the next-page links point instead of here
    expires_in: int = 60  # short, so that every sync asks for a new token


class GraphStandIn:
    def __init__(self, certificate: Path, key: Path) -> None:
        self.script = Script()
        self.requests: list[Recorded] = []
        self._tokens = 0
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self) -> None:
                parts = urlsplit(self.path)
                length = int(self.headers.get("Content-Length", 0))
                form = parse_qs(self.rfile.read(length).decode()) if length else {}
                stand_in.requests.append(
                    Recorded(
                        self.command,
                        parts.path,
                        parse_qs(parts.query),
                        {name.lower(): value for name, value in self.headers.items()},
                        form,
                    )
                )
                status, body = stand_in.answer(self.command, parts.path, parts.query, form, self)
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _handle

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, key)
        self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base(self) -> str:
        return f"https://127.0.0.1:{self._server.server_address[1]}"

    @property
    def authority(self) -> str:
        return f"{self.base}/{TENANT}"

    @property
    def graph(self) -> str:
        return f"{self.base}/v1.0"

    def reset(self) -> None:
        """Back to the default script and no requests. Token numbers go on counting, so that a
        token from an earlier test is never mistaken for a new one."""
        self.script = Script()
        self.requests = []

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def token_requests(self) -> list[Recorded]:
        return [r for r in self.requests if r.path.endswith("/oauth2/v2.0/token")]

    def graph_requests(self) -> list[Recorded]:
        return [r for r in self.requests if r.path.startswith("/v1.0/")]

    def answer(
        self,
        method: str,
        path: str,
        query: str,
        form: dict[str, list[str]],
        handler: BaseHTTPRequestHandler,
    ) -> tuple[int, bytes]:
        if path.startswith("/v1.0/"):
            return self._graph(method, path, query, handler)
        base = f"/{TENANT}"
        if method == "GET" and path == f"{base}/v2.0/.well-known/openid-configuration":
            return 200, self._json(
                {
                    "issuer": self.authority,
                    "authorization_endpoint": f"{self.authority}/oauth2/v2.0/authorize",
                    "token_endpoint": f"{self.authority}/oauth2/v2.0/token",
                    "device_authorization_endpoint": f"{self.authority}/oauth2/v2.0/devicecode",
                }
            )
        if method == "POST" and path == f"{base}/oauth2/v2.0/devicecode":
            return 200, self._json(
                {
                    "device_code": "device-code",
                    "user_code": "USER-CODE",
                    "verification_uri": f"{self.base}/device",
                    "expires_in": 900,
                    "interval": 1,
                    "message": "To sign in, open the stand-in's device page and enter USER-CODE.",
                }
            )
        if method == "POST" and path == f"{base}/oauth2/v2.0/token":
            return self._token(form)
        return 404, b"{}"

    def _token(self, form: dict[str, list[str]]) -> tuple[int, bytes]:
        if self.script.token_body is not None:
            return self.script.token_status, self.script.token_body
        grant = form.get("grant_type", [""])[0]
        if grant not in (DEVICE_GRANT, "refresh_token"):
            return 400, self._json({"error": "unsupported_grant_type"})
        self._tokens += 1
        client_info = _segment({"uid": "user-1", "utid": TENANT})
        return 200, self._json(
            {
                "token_type": "Bearer",
                "scope": self.script.granted_scope,
                "expires_in": self.script.expires_in,
                "ext_expires_in": self.script.expires_in,
                "access_token": f"access-{self._tokens}",
                "refresh_token": f"refresh-{self._tokens}",
                "id_token": id_token(),
                "client_info": client_info,
            }
        )

    def _graph(
        self, method: str, path: str, query: str, handler: BaseHTTPRequestHandler
    ) -> tuple[int, bytes]:
        if method != "GET" or path != "/v1.0/me/calendarView":
            return 405, b"{}"
        if self.script.graph_body is not None:
            return self.script.graph_status, self.script.graph_body
        authorization = handler.headers.get("Authorization", "")
        if not authorization.startswith("Bearer access-"):
            return 401, self._json({"error": {"code": "InvalidAuthenticationToken"}})
        if self.script.graph_status != 200:
            return self.script.graph_status, self._json({"error": {"code": "Failed"}})
        skip = int(parse_qs(query).get("skip", ["0"])[0])
        size = self.script.page_size
        page: dict[str, object] = {"value": self.script.events[skip : skip + size]}
        if skip + size < len(self.script.events):
            link_base = self.script.next_link_base or self.graph
            page["@odata.nextLink"] = f"{link_base}/me/calendarView?skip={skip + size}"
        return 200, self._json(page)

    @staticmethod
    def _json(value: object) -> bytes:
        return json.dumps(value).encode()
