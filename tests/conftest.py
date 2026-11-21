import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

AGENT_TOKEN = "test-agent-token-0123456789abcdef"
# The clock the gate runs on in tests, so the 30-day window is the same on every day.
# Fixture mail is dated within the 30 days before it.
NOW = "2026-01-10T12:00:00Z"


@dataclass(frozen=True)
class RunningGate:
    url: str
    token: str
    db: Path


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def free_port() -> int:
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


@contextmanager
def run_gate(
    db: Path,
    *,
    mail_fixture: Path | None = None,
    mail_url: str | None = None,
    token: str = AGENT_TOKEN,
    now: str = NOW,
) -> Iterator[RunningGate]:
    """Run the service through its entry point on a free loopback port.

    The database, the one mail source and the clock are the only things a test
    chooses; the rest of the configuration is whatever the entry point defaults to.
    """
    port = free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("GLOVEBOX_")}
    env.update(
        GLOVEBOX_HOST="127.0.0.1",
        GLOVEBOX_PORT=str(port),
        GLOVEBOX_AGENT_TOKEN=token,
        GLOVEBOX_DB=str(db),
        GLOVEBOX_FIXED_NOW=now,
    )
    if mail_fixture is not None:
        env["GLOVEBOX_MAIL_FIXTURE"] = str(mail_fixture)
    if mail_url is not None:
        env["GLOVEBOX_STANDIN_MAILBOX_MAIL"] = mail_url
    process = subprocess.Popen([sys.executable, "-m", "glovebox"], env=env)
    try:
        deadline = time.monotonic() + 15
        while not _accepts_connections(port):
            if process.poll() is not None or time.monotonic() > deadline:
                raise RuntimeError("the service did not start")
            time.sleep(0.05)
        yield RunningGate(url=f"http://127.0.0.1:{port}/mcp", token=token, db=db)
    finally:
        process.terminate()
        process.wait(timeout=10)


GateStarter = Callable[..., AbstractContextManager[RunningGate]]


@pytest.fixture
def start_gate(tmp_path: Path) -> GateStarter:
    """Start gates against a database under the test's temporary directory.

    Starting twice with the same `db` name is a restart of the same service.
    """

    def start(
        db: str = "glovebox.db",
        *,
        mail_fixture: Path | None = None,
        mail_url: str | None = None,
        token: str = AGENT_TOKEN,
        now: str = NOW,
    ) -> AbstractContextManager[RunningGate]:
        return run_gate(
            tmp_path / db, mail_fixture=mail_fixture, mail_url=mail_url, token=token, now=now
        )

    return start


@pytest.fixture
def gate(start_gate: GateStarter) -> Iterator[RunningGate]:
    """A running service with no mail source configured."""
    with start_gate() as running:
        yield running
