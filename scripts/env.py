#!/usr/bin/env python3
"""Bring up, check, reseed and remove a Glovebox environment stack (ADR 0007).

Throwaway runs in Docker and shares compose.yaml with the Linux test tier; test
adds compose.test.yaml. On macOS the test tier runs natively instead: a virtualenv
from the checkout, started by launchd daemons that run `serve` (no Docker). See
docs/environments.md for the tiers, the data rule and the production runbook.

Status output is fixed text only: it never echoes a response body, an exception
message or a secret.
"""

import argparse
import fcntl
import getpass
import hashlib
import http.client
import json
import os
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from xml.sax.saxutils import escape

SCRIPT = Path(__file__).resolve()
REPO = SCRIPT.parent.parent
ENV_FILE = REPO / ".env"
LOCK_FILE = REPO / ".env.lock"
UNIT_SOURCE = REPO / "deploy" / "systemd"
DEFAULT_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"
PLIST_SOURCE = REPO / "deploy" / "launchd"
DEFAULT_PLIST_DIR = Path.home() / "glovebox-launchd"
# Native backend files, all owned by the tier's user and gitignored.
VENV = REPO / ".venv"
RUN_DIR = REPO / ".run"
SEED_STATE = RUN_DIR / "state" / "seed"
DESIRED_FILE = RUN_DIR / "desired"
INSTALL_STAMP = VENV / ".glovebox-install"

TEST_USER = "glovebox-test"
TEST_PROJECT = "glovebox-test"
GATE_PORT = 8765
TOKEN_VAR = "GLOVEBOX_TEST_TOKEN"
TIMER = "glovebox-test-env.timer"
SERVICE = "glovebox-test-env.service"
TIERS = ("throwaway", "test")
STACK_SERVICES = ("gate", "standins")
INITIALIZE_ID = 1  # the JSON-RPC id of the initialize request `check` sends

# The stand-ins' ports and the variables that tell the gate where they answer. They
# mirror compose.yaml, where the host is `standins`; natively it is loopback.
STANDIN_VARS = {
    "GLOVEBOX_STANDIN_MAILBOX_MAIL": 9101,
    "GLOVEBOX_STANDIN_MAILBOX_CALENDAR": 9102,
    "GLOVEBOX_STANDIN_O365_MAIL": 9103,
    "GLOVEBOX_STANDIN_O365_CALENDAR": 9104,
    "GLOVEBOX_STANDIN_SIGNAL": 9105,
    "GLOVEBOX_STANDIN_IMESSAGE": 9106,
    "GLOVEBOX_STANDIN_WHATSAPP": 9107,
    "GLOVEBOX_STANDIN_INFERENCE_LOCAL": 9108,
    "GLOVEBOX_STANDIN_INFERENCE_EXTERNAL": 9109,
}
MIN_PYTHON = (3, 12)  # requires-python in pyproject.toml
NATIVE_SERVICES = ("gate", "standins")
NATIVE_WAIT_SECONDS = 90
STOP_GRACE_SECONDS = 10
SERVE_POLL_SECONDS = 1.0

# The system LaunchDaemons of the native test tier, in deploy/launchd/. Each file's
# Label is its name without `.plist`.
TEST_PLISTS = (
    "glovebox.test.gate.plist",
    "glovebox.test.standins.plist",
    "glovebox.test.ensure-test.plist",
)

Handler = Callable[[argparse.Namespace], int]


def resolve_tier(requested: str | None) -> str:
    tier = requested or os.environ.get("GLOVEBOX_ENV_TIER")
    if tier is None:
        tier = "test" if getpass.getuser() == TEST_USER else "throwaway"
    if tier not in TIERS:
        raise SystemExit(f"unknown tier, expected one of: {', '.join(TIERS)}")
    return tier


def project_name(tier: str) -> str:
    override = os.environ.get("GLOVEBOX_ENV_PROJECT")
    if override:
        return override
    if tier == "test":
        return TEST_PROJECT
    return "glovebox-" + hashlib.sha1(str(REPO).encode()).hexdigest()[:8]


def platform_backend() -> str:
    """`native` on macOS, else `docker`; GLOVEBOX_ENV_BACKEND overrides it (for tests)."""
    forced = os.environ.get("GLOVEBOX_ENV_BACKEND")
    if forced not in (None, "docker", "native"):
        raise SystemExit("unknown backend, expected one of: docker, native")
    return forced or ("native" if sys.platform == "darwin" else "docker")


def backend(tier: str) -> str:
    """Only the test tier runs natively; throwaway always runs in Docker."""
    return platform_backend() if tier == "test" else "docker"


def ensure_env_file(path: Path) -> bool:
    """Create the env file with a fresh test token; leave an existing file alone."""
    # Write a private temporary file, then link it into place: the env file
    # appears complete or not at all, and an existing file is never replaced.
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(f"{TOKEN_VAR}={secrets.token_urlsafe(32)}\n")
    try:
        os.link(temporary, path)
    except FileExistsError:
        return False
    finally:
        temporary.unlink()
    return True


def read_env_file() -> dict[str, str]:
    """The KEY=VALUE lines of the env file; empty when it is missing or unreadable."""
    try:
        lines = ENV_FILE.read_text().splitlines()
    except (OSError, ValueError):  # missing, unreadable, or not text
        return {}
    pairs = (line.partition("=") for line in lines)
    return {key: value for key, _, value in pairs if value}


def read_token(name: str) -> str | None:
    """The token from the environment, else from the env file."""
    return os.environ.get(name) or read_env_file().get(name)


def compose_command(tier: str, *args: str) -> list[str]:
    command = ["docker", "compose", "-p", project_name(tier)]
    if ENV_FILE.exists():
        command += ["--env-file", str(ENV_FILE)]
    command += ["-f", "compose.yaml"]
    if tier == "test":
        command += ["-f", "compose.test.yaml"]
    return [*command, *args]


def compose(tier: str, *args: str, capture: bool = False) -> subprocess.CompletedProcess[str]:
    # Only `up` creates the env file. Commands that run without it (down, reset,
    # ps, port) never start the gate, so a placeholder satisfies compose.
    env = {**os.environ}
    if not ENV_FILE.exists():
        env.setdefault(TOKEN_VAR, "unused")
    sys.stdout.flush()  # keep our lines in order with compose's output
    command = compose_command(tier, *args)
    try:
        return subprocess.run(
            command, cwd=REPO, env=env, text=True, capture_output=capture, check=False
        )
    except OSError:  # no docker on PATH
        return subprocess.CompletedProcess(command, 127, "", "")


def step(label: str, result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode != 0:
        print(f"{label}: failed")
        return False
    return True


def seed(tier: str) -> bool:
    return step("seed", compose(tier, "run", "--rm", "--no-deps", "seed"))


# The native backend (macOS test tier). The venv, the seeded state and the desired
# state all live under the checkout and belong to the tier's user, so nothing here
# needs root. The launchd daemons run `serve`, which keeps one service's process
# alive while DESIRED_FILE asks for it; `up`, `down` and `ensure-test` only change
# that file, never launchd.


def run_command(
    command: list[str], *, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    sys.stdout.flush()  # keep our lines in order with the command's output
    try:
        return subprocess.run(command, cwd=REPO, env=env, text=True, check=False)
    except OSError:  # the program is not there
        return subprocess.CompletedProcess(command, 127, "", "")


def native_gate_port() -> int:
    raw = os.environ.get("GLOVEBOX_ENV_PUBLISH_PORT", str(GATE_PORT))
    if not raw.isdecimal():
        raise SystemExit("GLOVEBOX_ENV_PUBLISH_PORT must be a port number")
    return int(raw)


def venv_python() -> Path:
    return VENV / "bin" / "python"


def service_plan(service: str) -> tuple[list[str], dict[str, str]]:
    """The command and environment that run one native service.

    Everything binds loopback only, like the Docker test tier (the gate has no
    authentication yet). The stand-ins are reached by their loopback ports.
    """
    env = {**os.environ, **read_env_file()}
    if service == "gate":
        env["GLOVEBOX_HOST"] = "127.0.0.1"
        env["GLOVEBOX_PORT"] = str(native_gate_port())
        for name, port in STANDIN_VARS.items():
            env[name] = f"http://127.0.0.1:{port}"
        return [str(VENV / "bin" / "glovebox")], env
    env["STANDINS_HOST"] = "127.0.0.1"
    env["STANDINS_STATE"] = str(SEED_STATE)
    return [str(venv_python()), str(REPO / "deploy" / "standins" / "standins.py")], env


def dependency_digest() -> str:
    digest = hashlib.sha256()
    for name in ("pyproject.toml", "constraints.txt"):
        digest.update((REPO / name).read_bytes())
    return digest.hexdigest()


def release_fingerprint() -> str:
    """Changes when the checked-out commit or the pinned dependencies change."""
    head = git("rev-parse", "HEAD").stdout.strip() or "nogit"
    return f"{head}-{dependency_digest()[:12]}"


def install_dependencies() -> bool:
    """Create the venv if missing and install the checkout into it when the pins changed."""
    if not venv_python().exists():
        if not step("venv", run_command([sys.executable, "-m", "venv", str(VENV)])):
            return False
    digest = dependency_digest()
    try:
        if INSTALL_STAMP.read_text() == digest:
            return True
    except OSError:
        pass
    install = [str(venv_python()), "-m", "pip", "install", "--quiet", "-c", "constraints.txt"]
    if not step("install", run_command([*install, "-e", "."])):
        return False
    INSTALL_STAMP.write_text(digest)
    return True


def write_desired(text: str) -> None:
    RUN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = DESIRED_FILE.with_name(f"{DESIRED_FILE.name}.{os.getpid()}.tmp")
    temporary.write_text(text + "\n")
    os.replace(temporary, DESIRED_FILE)


def read_desired() -> str:
    try:
        return DESIRED_FILE.read_text().strip()
    except OSError:
        return ""


def pid_file(service: str) -> Path:
    return RUN_DIR / f"{service}.pid"


def pid_alive(path: Path) -> bool:
    try:
        os.kill(int(path.read_text()), 0)
    except (OSError, ValueError):  # no file, no such process, or not a pid
        return False
    return True


def probe_health(port: int) -> str:
    """GET a stand-in's /healthz: ok, unreachable or unhealthy."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/healthz", timeout=3):
            return "ok"
    except urllib.error.HTTPError:
        return "unhealthy"
    except (urllib.error.URLError, OSError):
        return "unreachable"
    except http.client.HTTPException:
        return "unhealthy"


def native_standins_status() -> str:
    outcomes = {probe_health(port) for port in STANDIN_VARS.values()}
    if outcomes == {"ok"}:
        return "ok"
    return "missing" if outcomes == {"unreachable"} else "unhealthy"


def native_status() -> tuple[str, str]:
    """The gate's and the stand-ins' status words, probed over loopback."""
    url = f"http://127.0.0.1:{native_gate_port()}/mcp"
    return probe_gate(url, read_token(TOKEN_VAR)), native_standins_status()


def native_seed() -> bool:
    RUN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    seeding = [sys.executable, str(REPO / "deploy" / "standins" / "seed.py")]
    seeding += ["--fixtures", str(REPO / "fixtures" / "cast.json"), "--state", str(SEED_STATE)]
    return step("seed", run_command(seeding))


def native_up() -> int:
    if sys.version_info < MIN_PYTHON:
        print("up: python 3.12 or newer is required")
        return 1
    if not install_dependencies() or not native_seed():
        return 1
    write_desired(f"up {release_fingerprint()}")
    deadline = time.monotonic() + NATIVE_WAIT_SECONDS
    while native_status() != ("ok", "ok"):
        if time.monotonic() > deadline:
            print("start: failed")
            return 1
        time.sleep(1)
    return 0


def native_down() -> int:
    write_desired("down")
    deadline = time.monotonic() + STOP_GRACE_SECONDS * 3
    while any(pid_alive(pid_file(service)) for service in NATIVE_SERVICES):
        if time.monotonic() > deadline:
            print("stop: failed")
            return 1
        time.sleep(0.2)
    shutil.rmtree(SEED_STATE.parent, ignore_errors=True)
    return 0


def stop_process(process: subprocess.Popen[bytes]) -> None:
    process.terminate()
    try:
        process.wait(timeout=STOP_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def reexec(argv: list[str]) -> None:
    os.execv(sys.executable, [sys.executable, *argv])


def serve(service: str, argv: list[str], poll: float = SERVE_POLL_SECONDS) -> int:
    """Run one service in the foreground, as a launchd daemon does.

    The process runs while DESIRED_FILE says `up ...`, restarts when that line
    changes or the process dies, and stops when the line says `down`. If env.py
    itself changes (ensure-test moved the checkout), it stops the service and
    re-executes the new script, keeping its pid, so launchd sees no restart.
    """
    stopping = False

    def request_stop(signum: int, frame: object) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    loaded = SCRIPT.stat().st_mtime_ns
    process: subprocess.Popen[bytes] | None = None
    running_for = ""
    pid = pid_file(service)

    def stop() -> None:
        nonlocal process
        if process is not None:
            stop_process(process)
            process = None
        pid.unlink(missing_ok=True)

    while not stopping:
        wanted = read_desired()
        if process is not None and process.poll() is not None:
            print(f"serve: {service} exited with code {process.returncode}", flush=True)
            stop()
        elif process is not None and wanted != running_for:
            stop()
        if process is None and wanted.startswith("up") and venv_python().exists():
            command, env = service_plan(service)
            try:
                process = subprocess.Popen(command, cwd=REPO, env=env)
            except OSError:
                print(f"serve: {service} did not start", flush=True)
            else:
                running_for = wanted
                RUN_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
                pid.write_text(f"{process.pid}\n")
        if SCRIPT.stat().st_mtime_ns != loaded:
            stop()
            reexec(argv)
        time.sleep(poll)
    stop()
    return 0


def command_up(args: argparse.Namespace) -> int:
    tier = resolve_tier(args.tier)
    if ensure_env_file(ENV_FILE):
        print("up: created .env with a new test token")
    if backend(tier) == "native":
        if native_up() != 0:
            return 1
    else:
        if not step("pull", compose(tier, "pull", "--ignore-buildable", "--quiet")):
            return 1
        if not step("build", compose(tier, "build", "--quiet")):
            return 1
        if not seed(tier):
            return 1
        if not step("start", compose(tier, "up", "--detach", "--wait", "--remove-orphans")):
            return 1
    print("up: ok")
    return 0


def command_reset(args: argparse.Namespace) -> int:
    tier = resolve_tier(args.tier)
    # The native stand-ins read the state directory on every request: no restart.
    if not (native_seed() if backend(tier) == "native" else seed(tier)):
        return 1
    print("reset: ok")
    return 0


def command_down(args: argparse.Namespace) -> int:
    tier = resolve_tier(args.tier)
    if backend(tier) == "native":
        if native_down() != 0:
            return 1
    elif not step("down", compose(tier, "down", "--volumes", "--remove-orphans")):
        return 1
    print("down: ok")
    return 0


def command_serve(args: argparse.Namespace) -> int:
    tier = resolve_tier(args.tier)
    if tier != "test" or backend(tier) != "native":
        print("serve: runs on the native test backend only")
        return 2
    return serve(args.service, [str(SCRIPT), "serve", args.service, "--tier", tier])


def probe_gate(url: str, token: str | None) -> str:
    """Send an MCP initialize request; return ok, unreachable or unhealthy."""
    payload = {
        "jsonrpc": "2.0",
        "id": INITIALIZE_ID,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "glovebox-env-check", "version": "0"},
        },
    }
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
            return "unhealthy"
        request = urllib.request.Request(
            url, data=json.dumps(payload).encode(), headers=headers, method="POST"
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError:
        return "unhealthy"
    except (urllib.error.URLError, OSError):
        return "unreachable"
    except (http.client.HTTPException, ValueError):
        # A reply that is not valid HTTP, or a URL that does not parse.
        return "unhealthy"
    return (
        "ok" if any(_is_initialize_reply(message) for message in _messages(body)) else "unhealthy"
    )


def _parse_json(text: str) -> object | None:
    try:
        parsed: object = json.loads(text)
    except (ValueError, RecursionError):  # not JSON, or nested too deeply to parse
        return None
    return parsed


def _messages(body: str) -> list[object]:
    """The JSON messages in a reply: the whole body, or the `data:` lines of an event stream."""
    whole = _parse_json(body)
    if whole is not None:
        return [whole]
    lines = (line.removeprefix("data:") for line in body.splitlines() if line.startswith("data:"))
    return [message for message in map(_parse_json, lines) if message is not None]


def _is_initialize_reply(message: object) -> bool:
    """True for a successful JSON-RPC reply to our initialize request, from `glovebox`."""
    if not isinstance(message, dict):
        return False
    request_id = message.get("id")
    if message.get("jsonrpc") != "2.0" or type(request_id) is not int or "error" in message:
        return False
    if request_id != INITIALIZE_ID:
        return False
    result = message.get("result")
    if not isinstance(result, dict):
        return False
    info = result.get("serverInfo")
    return (
        isinstance(result.get("protocolVersion"), str)
        and isinstance(result.get("capabilities"), dict)
        and isinstance(info, dict)
        and info.get("name") == "glovebox"
        and isinstance(info.get("version"), str)
    )


def stack_status(tier: str) -> dict[str, str]:
    """Map each stack service to running-and-healthy or a fixed failure word."""
    result = compose(tier, "ps", "--all", "--format", "json", capture=True)
    rows: list[dict[str, object]] = []
    if result.returncode == 0:
        text = result.stdout.strip()
        try:
            if text.startswith("["):
                rows = json.loads(text)
            else:
                rows = [json.loads(line) for line in text.splitlines() if line.strip()]
        except ValueError:
            rows = []
    found = {str(row.get("Service")): row for row in rows if isinstance(row, dict)}
    status: dict[str, str] = {}
    for name in STACK_SERVICES:
        row = found.get(name)
        if row is None:
            status[name] = "missing"
        elif row.get("State") == "running" and row.get("Health") in ("healthy", ""):
            status[name] = "ok"
        else:
            status[name] = "unhealthy"
    return status


def local_gate_url(tier: str) -> str | None:
    result = compose(tier, "port", "gate", str(GATE_PORT), capture=True)
    address = result.stdout.strip()
    if result.returncode != 0 or ":" not in address:
        return None
    port = address.rsplit(":", 1)[1]
    return f"http://127.0.0.1:{port}/mcp"


def command_check(args: argparse.Namespace) -> int:
    standins = "ok"
    if args.url is not None:
        gate = probe_gate(args.url, os.environ.get(args.token_env))
    else:
        tier = resolve_tier(args.tier)
        if backend(tier) == "native":
            gate, standins = native_status()
        else:
            token = read_token(TOKEN_VAR)
            status = stack_status(tier)
            standins = status["standins"]
            url = local_gate_url(tier)
            if status["gate"] != "ok":
                gate = status["gate"]
            elif url is None:
                gate = "unreachable"
            else:
                gate = probe_gate(url, token)
        print(f"standins: {standins}")
    print(f"gate: {gate}")
    healthy = gate == "ok" and standins == "ok"
    print("check: healthy" if healthy else "check: unhealthy")
    return 0 if healthy else 1


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=REPO, text=True, capture_output=True, check=False)


def command_ensure_test(args: argparse.Namespace) -> int:
    tier = resolve_tier(args.tier)
    if tier != "test":
        # It moves the checkout to origin/main, which would destroy a ticket's work.
        print("ensure-test: runs on the test tier only")
        return 2
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("ensure-test: another run is active")
            return 0
        if git("status", "--porcelain", "--untracked-files=no").stdout.strip():
            print("ensure-test: the checkout has local changes")
            return 1
        fetched = git("fetch", "--quiet", "origin", "+refs/heads/main:refs/remotes/origin/main")
        if fetched.returncode != 0:
            print("ensure-test: fetch failed")
            return 1
        if git("checkout", "--quiet", "--detach", "origin/main").returncode != 0:
            print("ensure-test: checkout failed")
            return 1
        # Run the updated script, not this one, so a changed env.py takes effect.
        script = str(REPO / "scripts" / "env.py")
        for command in ("up", "check"):
            code = subprocess.run(
                [sys.executable, script, command, "--tier", "test"], cwd=REPO, check=False
            ).returncode
            if code != 0:
                return code
    revision = git("rev-parse", "--short", "HEAD").stdout.strip()
    print(f"ensure-test: at {revision}")
    return 0


def user_home(user: str) -> Path:
    return Path.home() if getpass.getuser() == user else Path("/Users") / user


def render_plist(name: str, *, user: str, python: str, repo: Path, home: Path, logs: Path) -> str:
    """A deploy/launchd template with its @PLACEHOLDERS@ filled in and XML-escaped."""
    values = {"USER": user, "PYTHON": python, "REPO": str(repo), "HOME": str(home)}
    values["LOGS"] = str(logs)
    text = (PLIST_SOURCE / name).read_text()
    for key, value in values.items():
        text = text.replace(f"@{key}@", escape(value))
    return text


def install_launchd_jobs(args: argparse.Namespace) -> int:
    """Write the test tier's LaunchDaemon plists. Root installs them, once, by hand."""
    if args.enable:
        print("install-timer: launchd jobs are installed by root; use --no-enable --unit-dir DIR")
        return 2
    home = user_home(args.user)
    logs = home / "Library" / "Logs" / "glovebox-test"
    try:
        # launchd opens the log files but does not create their directory.
        logs.mkdir(parents=True, exist_ok=True)
    except OSError:
        print(f"install-timer: could not create the log directory; create it as {args.user}")
    unit_dir = Path(args.unit_dir or DEFAULT_PLIST_DIR)
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name in TEST_PLISTS:
        plist = render_plist(
            name, user=args.user, python=args.python, repo=REPO, home=home, logs=logs
        )
        (unit_dir / name).write_text(plist)
    print("install-timer: ok")
    return 0


def command_install_timer(args: argparse.Namespace) -> int:
    if platform_backend() == "native":
        return install_launchd_jobs(args)
    if args.enable and resolve_tier(args.tier) != "test":
        print("install-timer: enabling runs on the test tier only; use --no-enable here")
        return 2
    unit_dir = Path(args.unit_dir or DEFAULT_UNIT_DIR)
    unit_dir.mkdir(parents=True, exist_ok=True)
    service = (UNIT_SOURCE / SERVICE).read_text().replace("@REPO@", str(REPO))
    (unit_dir / SERVICE).write_text(service)
    (unit_dir / TIMER).write_text((UNIT_SOURCE / TIMER).read_text())
    if args.enable:
        for command in (["daemon-reload"], ["enable", "--now", TIMER]):
            try:
                code = subprocess.run(["systemctl", "--user", *command], check=False).returncode
            except OSError:  # no systemctl on PATH
                code = 127
            if code != 0:
                print("install-timer: systemctl failed")
                return 1
    print("install-timer: ok")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name: str, handler: Handler, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument(
            "--tier",
            choices=TIERS,
            help="default: test for the glovebox-test user (or GLOVEBOX_ENV_TIER), else throwaway",
        )
        sub.set_defaults(handler=handler)
        return sub

    add("up", command_up, "create the env file, pull, build, seed and start the stack")
    check = add("check", command_check, "exit non-zero unless the gate is healthy")
    check.add_argument("--url", help="check this gate MCP URL instead of the local stack")
    check.add_argument(
        "--token-env",
        default=TOKEN_VAR,
        help=f"environment variable holding the bearer token (default {TOKEN_VAR})",
    )
    add("reset", command_reset, "reseed the fictional fixture cast")
    add("down", command_down, "remove the stack and its volumes")
    add("ensure-test", command_ensure_test, "bring the test stack to the current origin/main")
    serve_parser = add("serve", command_serve, "run one native service in the foreground (launchd)")
    serve_parser.add_argument("service", choices=NATIVE_SERVICES)
    timer = add("install-timer", command_install_timer, f"install and start {TIMER}")
    timer.add_argument(
        "--unit-dir",
        help=f"where to write the units (default {DEFAULT_UNIT_DIR}, on macOS {DEFAULT_PLIST_DIR})",
    )
    timer.add_argument(
        "--no-enable",
        dest="enable",
        action="store_false",
        help="write the units only; do not call systemctl (required on macOS)",
    )
    timer.add_argument(
        "--user", default=TEST_USER, help="macOS: the user the daemons run as (default %(default)s)"
    )
    timer.add_argument(
        "--python",
        default=sys.executable,
        help="macOS: the Python 3.12+ that runs env.py in the daemons (default: this one)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Handler = args.handler
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
