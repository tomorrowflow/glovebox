#!/usr/bin/env python3
"""Bring up, check, reseed and remove a Glovebox environment stack (ADR 0007).

Throwaway and test share compose.yaml; test adds compose.test.yaml. See
docs/environments.md for the tiers, the data rule and the production runbook.

Status output is fixed text only: it never echoes a response body, an exception
message or a secret.
"""

import argparse
import fcntl
import getpass
import hashlib
import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_FILE = REPO / ".env"
LOCK_FILE = REPO / ".env.lock"
UNIT_SOURCE = REPO / "deploy" / "systemd"
DEFAULT_UNIT_DIR = Path.home() / ".config" / "systemd" / "user"

TEST_USER = "glovebox-test"
TEST_PROJECT = "glovebox-test"
GATE_PORT = 8765
TOKEN_VAR = "GLOVEBOX_TEST_TOKEN"
TIMER = "glovebox-test-env.timer"
SERVICE = "glovebox-test-env.service"
TIERS = ("throwaway", "test")
STACK_SERVICES = ("gate", "standins")

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


def ensure_env_file(path: Path) -> bool:
    """Create the env file with a fresh test token; leave an existing file alone."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as handle:
        handle.write(f"{TOKEN_VAR}={secrets.token_urlsafe(32)}\n")
    return True


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
    return subprocess.run(
        compose_command(tier, *args),
        cwd=REPO,
        env=env,
        text=True,
        capture_output=capture,
        check=False,
    )


def step(label: str, result: subprocess.CompletedProcess[str]) -> bool:
    if result.returncode != 0:
        print(f"{label}: failed")
        return False
    return True


def seed(tier: str) -> bool:
    return step("seed", compose(tier, "run", "--rm", "--no-deps", "seed"))


def command_up(args: argparse.Namespace) -> int:
    tier = resolve_tier(args.tier)
    if ensure_env_file(ENV_FILE):
        print("up: created .env with a new test token")
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
    if not seed(tier):
        return 1
    print("reset: ok")
    return 0


def command_down(args: argparse.Namespace) -> int:
    tier = resolve_tier(args.tier)
    if not step("down", compose(tier, "down", "--volumes", "--remove-orphans")):
        return 1
    print("down: ok")
    return 0


def probe_gate(url: str, token: str | None) -> str:
    """Send an MCP initialize request; return ok, unreachable or unhealthy."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
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
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            body = response.read(65536).decode("utf-8", "replace")
    except urllib.error.HTTPError:
        return "unhealthy"
    except (urllib.error.URLError, OSError):
        return "unreachable"
    return "ok" if _is_glovebox_initialize(body) else "unhealthy"


def _is_glovebox_initialize(body: str) -> bool:
    for line in body.splitlines():
        text = line.removeprefix("data:").strip() if line.startswith("data:") else line.strip()
        try:
            message = json.loads(text)
        except ValueError:
            continue
        if isinstance(message, dict):
            result = message.get("result")
            if isinstance(result, dict):
                info = result.get("serverInfo")
                return isinstance(info, dict) and info.get("name") == "glovebox"
    return False


def stack_status(tier: str) -> dict[str, str]:
    """Map each stack service to running-and-healthy or a fixed failure word."""
    result = compose(tier, "ps", "--all", "--format", "json", capture=True)
    rows: list[dict[str, object]] = []
    if result.returncode == 0:
        text = result.stdout.strip()
        if text.startswith("["):
            rows = json.loads(text)
        else:
            rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    found = {str(row.get("Service")): row for row in rows}
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
    token = os.environ.get(args.token_env)
    standins = "ok"
    if args.url is not None:
        gate = probe_gate(args.url, token)
    else:
        tier = resolve_tier(args.tier)
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


def command_install_timer(args: argparse.Namespace) -> int:
    if args.enable and resolve_tier(args.tier) != "test":
        print("install-timer: enabling runs on the test tier only; use --no-enable here")
        return 2
    unit_dir = Path(args.unit_dir)
    unit_dir.mkdir(parents=True, exist_ok=True)
    service = (UNIT_SOURCE / SERVICE).read_text().replace("@REPO@", str(REPO))
    (unit_dir / SERVICE).write_text(service)
    (unit_dir / TIMER).write_text((UNIT_SOURCE / TIMER).read_text())
    if args.enable:
        for command in (["daemon-reload"], ["enable", "--now", TIMER]):
            result = subprocess.run(["systemctl", "--user", *command], check=False)
            if result.returncode != 0:
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
    timer = add("install-timer", command_install_timer, f"install and start {TIMER}")
    timer.add_argument("--unit-dir", default=str(DEFAULT_UNIT_DIR), help="where to write the units")
    timer.add_argument(
        "--no-enable",
        dest="enable",
        action="store_false",
        help="write the units only; do not call systemctl",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler: Handler = args.handler
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
