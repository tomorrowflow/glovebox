"""`scripts/env.py check` against a gate URL (ADR 0007)."""

import json
import os
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tests.conftest import RunningGate

ENV_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "env.py"
UNHEALTHY = "gate: unhealthy\ncheck: unhealthy\n"


def run_env(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ENV_SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def assert_status(result: subprocess.CompletedProcess[str], code: int, stdout: str) -> None:
    """Exit code and fixed status text only; nothing on stderr."""
    assert (result.returncode, result.stdout, result.stderr) == (code, stdout, "")


@contextmanager
def answering_with(body: bytes) -> Iterator[str]:
    """A local HTTP server whose every POST gets status 200 and this body."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp"
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def speaks_garbage() -> Iterator[str]:
    """A listener that answers every connection with bytes that are not HTTP."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()

    def answer() -> None:
        while True:
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            with connection:
                connection.recv(65536)
                connection.sendall(b"this is not http\r\n\r\n")

    threading.Thread(target=answer, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{listener.getsockname()[1]}/mcp"
    finally:
        listener.close()


def test_check_passes_against_a_running_gate(gate: RunningGate) -> None:
    env = {**os.environ, "GLOVEBOX_TEST_TOKEN": gate.token}

    assert_status(run_env("check", "--url", gate.url, env=env), 0, "gate: ok\ncheck: healthy\n")


def test_check_is_unhealthy_against_a_gate_that_refuses_its_token(gate: RunningGate) -> None:
    env = {**os.environ, "GLOVEBOX_TEST_TOKEN": "not-the-gates-token-0123456789"}

    assert_status(run_env("check", "--url", gate.url, env=env), 1, UNHEALTHY)


def test_check_fails_when_the_gate_is_unreachable() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]

    result = run_env("check", "--url", f"http://127.0.0.1:{closed_port}/mcp")

    assert_status(result, 1, "gate: unreachable\ncheck: unhealthy\n")


def with_changes(
    base: dict[str, object], drop: tuple[str, ...], changes: dict[str, object]
) -> dict[str, object]:
    """A copy of `base` with `changes` applied, then the keys in `drop` removed."""
    changed = {**base, **changes}
    for key in drop:
        del changed[key]
    return changed


def server_info(*, drop: tuple[str, ...] = (), **changes: object) -> dict[str, object]:
    """A valid `serverInfo` (an MCP Implementation: name and version), with changes applied."""
    return with_changes({"name": "glovebox", "version": "0"}, drop, changes)


def initialize_result(*, drop: tuple[str, ...] = (), **changes: object) -> dict[str, object]:
    """A valid MCP InitializeResult, with changes applied."""
    valid: dict[str, object] = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "serverInfo": server_info(),
    }
    return with_changes(valid, drop, changes)


def initialize_reply(**changes: object) -> bytes:
    """A well-formed reply to the initialize request `check` sends, with changes applied."""
    reply: dict[str, object] = {"jsonrpc": "2.0", "id": 1, "result": initialize_result()}
    reply.update(changes)
    return json.dumps(reply).encode()


@pytest.mark.parametrize(
    "body",
    [
        initialize_reply(),
        initialize_reply() + b"\n",
        json.dumps(json.loads(initialize_reply()), indent=2).encode(),
        b"event: message\ndata: " + initialize_reply() + b"\n\n",
        b": comment\n\nevent: message\r\ndata: " + initialize_reply() + b"\r\n\r\n",
        b"id: 7\nevent: message\ndata: " + initialize_reply() + b"\n\n",
        b'event: message\ndata: {"jsonrpc": "2.0", "method": "notifications/message"}\n\n'
        b"event: message\ndata: " + initialize_reply() + b"\n\n",
    ],
    ids=[
        "json",
        "json-newline",
        "json-indented",
        "sse",
        "sse-comment-crlf",
        "sse-id",
        "sse-notification-first",
    ],
)
def test_check_accepts_a_well_formed_initialize_reply(body: bytes) -> None:
    with answering_with(body) as url:
        result = run_env("check", "--url", url)

    assert_status(result, 0, "gate: ok\ncheck: healthy\n")


@pytest.mark.parametrize(
    "body",
    [
        b"a web page that is not an MCP server",
        b'{"result": {"serverInfo": {"name": "something-else"}}}',
        b'{"result": {"serverInfo": 5}}',
        b"[" * 60000,  # nested deeply enough to exhaust the JSON parser's recursion limit
        b"\xff\xfe not text",
        # Names glovebox but is not a reply to our initialize request:
        b'{"result": {"serverInfo": {"name": "glovebox"}}}',
        initialize_reply(jsonrpc="1.0"),
        initialize_reply(id=2),
        initialize_reply(id="1"),
        initialize_reply(id=True),
        initialize_reply(result=initialize_result(drop=("protocolVersion",))),
        initialize_reply(result=initialize_result(protocolVersion=1)),
        initialize_reply(result=initialize_result(drop=("capabilities",))),
        initialize_reply(result=initialize_result(capabilities=[])),
        initialize_reply(result=initialize_result(drop=("serverInfo",))),
        initialize_reply(result=initialize_result(serverInfo="glovebox")),
        initialize_reply(result=initialize_result(serverInfo=server_info(drop=("name",)))),
        initialize_reply(result=initialize_result(serverInfo=server_info(name="other"))),
        initialize_reply(result=initialize_result(serverInfo=server_info(drop=("version",)))),
        initialize_reply(result=initialize_result(serverInfo=server_info(version=1))),
        initialize_reply(error={"code": -32600, "message": "glovebox says no"}),
        b'{"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "glovebox"}}',
        b"[" + initialize_reply() + b"]",  # a batch is not a reply to a single request
        b'{"method": "notifications/message", "params": {"serverInfo": {"name": "glovebox"}}}',
    ],
    ids=[
        "web-page",
        "other-server",
        "wrong-shape",
        "deeply-nested",
        "not-utf8",
        "name-only",
        "wrong-jsonrpc-version",
        "wrong-id",
        "string-id",
        "bool-id",
        "no-protocol-version",
        "non-string-protocol-version",
        "no-capabilities",
        "non-dict-capabilities",
        "no-server-info",
        "non-dict-server-info",
        "no-server-name",
        "other-server-name",
        "no-server-version",
        "non-string-server-version",
        "result-and-error",
        "error-only",
        "batch",
        "notification",
    ],
)
def test_check_fails_when_the_endpoint_is_not_the_gate(body: bytes) -> None:
    with answering_with(body) as url:
        result = run_env("check", "--url", url)

    assert_status(result, 1, UNHEALTHY)


def test_check_reports_unhealthy_when_the_reply_is_not_http(speaks_garbage: str) -> None:
    assert_status(run_env("check", "--url", speaks_garbage), 1, UNHEALTHY)


@pytest.mark.parametrize("url", ["not a url", "ftp://127.0.0.1/mcp", "http://[::1"])
def test_check_reports_unhealthy_for_a_malformed_url(url: str) -> None:
    assert_status(run_env("check", "--url", url), 1, UNHEALTHY)


def test_check_prints_only_fixed_text_when_docker_is_missing(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(ENV_SCRIPT), "check", "--tier", "throwaway"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={"PATH": str(tmp_path)},
    )

    assert_status(result, 1, "standins: missing\ngate: missing\ncheck: unhealthy\n")
