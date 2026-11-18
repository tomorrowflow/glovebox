import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest


@dataclass(frozen=True)
class RunningGate:
    url: str


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _accepts_connections(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


@pytest.fixture
def gate() -> Iterator[RunningGate]:
    """Run the service through its entry point on a free loopback port for one test."""
    port = _free_port()
    env = {**os.environ, "GLOVEBOX_HOST": "127.0.0.1", "GLOVEBOX_PORT": str(port)}
    process = subprocess.Popen([sys.executable, "-m", "glovebox"], env=env)
    try:
        deadline = time.monotonic() + 15
        while not _accepts_connections(port):
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("the service did not start")
            time.sleep(0.05)
        yield RunningGate(url=f"http://127.0.0.1:{port}/mcp")
    finally:
        process.terminate()
        process.wait(timeout=10)
