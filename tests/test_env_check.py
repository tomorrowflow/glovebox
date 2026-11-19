"""`scripts/env.py check` against a gate URL (ADR 0007)."""

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


def run_env(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ENV_SCRIPT), *args],
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
    assert_status(run_env("check", "--url", gate.url), 0, "gate: ok\ncheck: healthy\n")


def test_check_fails_when_the_gate_is_unreachable() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]

    result = run_env("check", "--url", f"http://127.0.0.1:{closed_port}/mcp")

    assert_status(result, 1, "gate: unreachable\ncheck: unhealthy\n")


@pytest.mark.parametrize(
    "body",
    [
        b"a web page that is not an MCP server",
        b'{"result": {"serverInfo": {"name": "something-else"}}}',
        b'{"result": {"serverInfo": 5}}',
        b"[" * 60000,  # nested deeply enough to exhaust the JSON parser's recursion limit
        b"\xff\xfe not text",
    ],
    ids=["web-page", "other-server", "wrong-shape", "deeply-nested", "not-utf8"],
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
