"""`scripts/env.py check` against a gate URL (ADR 0007)."""

import socket
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tests.conftest import RunningGate

ENV_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "env.py"


def run_env(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ENV_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


class NotTheGate(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        body = b"a web page that is not an MCP server"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture
def not_the_gate() -> Iterator[str]:
    server = HTTPServer(("127.0.0.1", 0), NotTheGate)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/mcp"
    finally:
        server.shutdown()
        server.server_close()


def test_check_passes_against_a_running_gate(gate: RunningGate) -> None:
    result = run_env("check", "--url", gate.url)

    assert result.returncode == 0
    assert result.stdout == "gate: ok\ncheck: healthy\n"


def test_check_fails_when_the_gate_is_unreachable() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        closed_port = sock.getsockname()[1]

    result = run_env("check", "--url", f"http://127.0.0.1:{closed_port}/mcp")

    assert result.returncode == 1
    assert result.stdout == "gate: unreachable\ncheck: unhealthy\n"
    assert result.stderr == ""


def test_check_fails_when_the_endpoint_is_not_the_gate(not_the_gate: str) -> None:
    result = run_env("check", "--url", not_the_gate)

    assert result.returncode == 1
    assert result.stdout == "gate: unhealthy\ncheck: unhealthy\n"
