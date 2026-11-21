import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from glovebox.access import Access

# The clock the gate runs on in tests, so the 30-day window is the same on every day.
# Fixture mail is dated within the 30 days before it.
NOW = "2026-01-10T12:00:00Z"
DEFAULT_ACCOUNT = "fixture"
DEFAULT_TOKEN = "default"

# The token values this process issued, by database and label. A value is shown once, so
# a test that restarts a gate on the same database needs the harness to remember it.
_ISSUED: dict[Path, dict[str, str]] = {}


@dataclass(frozen=True)
class RunningGate:
    url: str
    token: str  # the value of the default token
    db: Path
    log: Path  # what the service wrote to stdout and stderr
    tokens: Mapping[str, str]  # every token the harness issued for this database, by label


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


def clean_environment(db: Path, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GLOVEBOX_")}
    env.update(GLOVEBOX_DB=str(db), GLOVEBOX_FIXED_NOW=NOW)
    env.update(extra)
    return env


def operator(db: Path, *args: str, **extra: str) -> subprocess.CompletedProcess[str]:
    """Run the operator's command line against `db`, as the operator would."""
    return subprocess.run(
        [sys.executable, "-m", "glovebox", *args],
        env=clean_environment(db, **extra),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def provision(
    db: Path, accounts: Mapping[str, Path | str], tokens: Mapping[str, Sequence[str]]
) -> dict[str, str]:
    """Add the accounts and issue the tokens that `db` does not hold yet, through the CLI.

    Idempotent per database, so that starting a gate twice on one database is a restart:
    an account that exists is kept (re-pointed if its source changed, below the CLI on
    purpose) and a token already issued keeps its value. Returns every token by label.
    """
    issued = _ISSUED.setdefault(db, {})
    existing = {
        fields[0]: fields[3]
        for line in operator(db, "account", "list").stdout.splitlines()
        if (fields := line.split("\t"))
    }
    for name, source in accounts.items():
        location = str(source) if str(source).startswith("http") else str(Path(source).absolute())
        if name not in existing:
            added = operator(
                db, "account", "add", name, "--kind", "fixture_mail", "--source", location
            )
            assert added.returncode == 0, added.stderr
        elif existing[name] != location:
            Access(db).set_source(name, location)
    for label, covered in tokens.items():
        if label in issued:
            continue
        arguments = [arg for name in covered for arg in ("--account", name)]
        result = operator(db, "token", "issue", *arguments, "--label", label)
        assert result.returncode == 0, result.stderr
        issued[label] = result.stdout.strip()
    return dict(issued)


@contextmanager
def run_gate(
    db: Path,
    *,
    accounts: Mapping[str, Path | str],
    tokens: Mapping[str, Sequence[str]] | None = None,
    default_token: str = DEFAULT_TOKEN,
    now: str = NOW,
    extra_env: Mapping[str, str] | None = None,
    command: Sequence[str] = (),
) -> Iterator[RunningGate]:
    """Run the service through its entry point on a free loopback port.

    The accounts and tokens are set up first with the operator's command line, the way
    an operator does it; then the database, the clock and the port are the only
    configuration the service gets.
    """
    wanted = tokens if tokens is not None else {default_token: list(accounts)}
    issued = provision(db, accounts, wanted)
    port = free_port()
    env = clean_environment(
        db, GLOVEBOX_HOST="127.0.0.1", GLOVEBOX_PORT=str(port), GLOVEBOX_FIXED_NOW=now
    )
    env.update(extra_env or {})
    log = db.parent / f"gate-{db.stem}.log"
    with log.open("ab") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "glovebox", *command], env=env, stdout=output, stderr=output
        )
        try:
            deadline = time.monotonic() + 15
            while not _accepts_connections(port):
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("the service did not start")
                time.sleep(0.05)
            token = issued.get(default_token) or next(iter(issued.values()), "")
            yield RunningGate(
                url=f"http://127.0.0.1:{port}/mcp", token=token, db=db, log=log, tokens=issued
            )
        finally:
            process.terminate()
            process.wait(timeout=10)


GateStarter = Callable[..., AbstractContextManager[RunningGate]]


@pytest.fixture
def start_gate(tmp_path: Path) -> GateStarter:
    """Start gates against a database under the test's temporary directory.

    With `mail_fixture` or `mail_url` the gate has one account, `fixture`, and one token,
    `default`, that covers it. With neither, the account's source is a file that does not
    exist. `accounts` (name to fixture file or stand-in URL) and `tokens` (label to the
    account names it covers) set up more. Starting twice with the same `db` name is a
    restart of the same service.
    """

    def start(
        db: str = "glovebox.db",
        *,
        mail_fixture: Path | None = None,
        mail_url: str | None = None,
        token: str = DEFAULT_TOKEN,
        accounts: Mapping[str, Path | str] | None = None,
        tokens: Mapping[str, Sequence[str]] | None = None,
        now: str = NOW,
    ) -> AbstractContextManager[RunningGate]:
        source: Path | str = mail_url or mail_fixture or tmp_path / "no-such-mail.json"
        return run_gate(
            tmp_path / db,
            accounts=accounts if accounts is not None else {DEFAULT_ACCOUNT: source},
            tokens=tokens,
            default_token=token,
            now=now,
        )

    return start


@pytest.fixture
def gate(start_gate: GateStarter) -> Iterator[RunningGate]:
    """A running service whose one account has no readable source."""
    with start_gate() as running:
        yield running
