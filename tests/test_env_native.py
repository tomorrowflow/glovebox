"""The native (macOS) backend of `scripts/env.py`: command plan, launchd plists, `serve` (ADR 0007).

Seam: the plists `install-timer` writes, the command and environment each service runs
with, the desired-state file that `up` and `down` change, and the `serve` loop that
acts on it. Nothing here needs macOS, launchd or root.
"""

import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import textwrap
import threading
import time
import xml.etree.ElementTree as ElementTree
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from glovebox.config import HOST_PATTERN, ConfigError, parse_allowed_hosts
from tests.test_env_file import ROOT, load_env_module
from tests.test_standins import STANDINS, load_module

LAUNCHD = ROOT / "deploy" / "launchd"
TEST_PLISTS = [
    "glovebox.test.gate.plist",
    "glovebox.test.standins.plist",
    "glovebox.test.ensure-test.plist",
]
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """The env module on the native backend, with every path it writes under tmp_path."""
    module = load_env_module()
    monkeypatch.setenv("GLOVEBOX_ENV_BACKEND", "native")
    monkeypatch.setenv("GLOVEBOX_ENV_TIER", "test")
    monkeypatch.delenv("GLOVEBOX_ENV_PUBLISH_PORT", raising=False)
    monkeypatch.setattr(module, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(module, "RUN_DIR", tmp_path / "run")
    monkeypatch.setattr(module, "DESIRED_FILE", tmp_path / "run" / "desired")
    monkeypatch.setattr(module, "SEED_STATE", tmp_path / "run" / "state" / "seed")
    monkeypatch.setattr(module, "VENV", tmp_path / "venv")
    monkeypatch.setattr(module, "INSTALL_STAMP", tmp_path / "venv" / ".glovebox-install")
    return module


def install_plists(
    env: ModuleType, tmp_path: Path, *extra: str, user_home: Path | None = None
) -> Path:
    unit_dir = tmp_path / "units"
    home = user_home or tmp_path / "home"
    setattr(env, "user_home", lambda user: home)  # noqa: B010 - env is a loaded module
    code = env.main(["install-timer", "--no-enable", "--unit-dir", str(unit_dir), *extra])
    assert code == 0
    return unit_dir


def load_plist(path: Path) -> dict[str, Any]:
    parsed: dict[str, Any] = plistlib.loads(path.read_bytes())
    return parsed


# --- the plists -------------------------------------------------------------------------


@pytest.mark.parametrize("name", TEST_PLISTS)
def test_a_test_plist_is_a_system_daemon_for_the_tier_user(
    env: ModuleType, tmp_path: Path, name: str
) -> None:
    unit_dir = install_plists(env, tmp_path, "--python", "/opt/homebrew/bin/python3")

    plist = load_plist(unit_dir / name)

    assert plist["Label"] == name.removesuffix(".plist")
    assert plist["UserName"] == "glovebox-test"
    assert plist["ProgramArguments"][:2] == ["/opt/homebrew/bin/python3", f"{ROOT}/scripts/env.py"]
    assert plist["WorkingDirectory"] == str(ROOT)
    assert plist["EnvironmentVariables"]["GLOVEBOX_ENV_TIER"] == "test"
    assert plist["EnvironmentVariables"]["HOME"] == str(tmp_path / "home")


@pytest.mark.parametrize("name", TEST_PLISTS)
def test_a_test_plist_logs_under_the_users_library_and_keeps_no_placeholder(
    env: ModuleType, tmp_path: Path, name: str
) -> None:
    unit_dir = install_plists(env, tmp_path)

    plist = load_plist(unit_dir / name)

    logs = tmp_path / "home" / "Library" / "Logs" / "glovebox-test"
    assert Path(plist["StandardOutPath"]).parent == logs
    assert Path(plist["StandardErrorPath"]).parent == logs
    assert logs.is_dir()  # launchd does not create it
    assert re.search(r"@[A-Z]+@", (unit_dir / name).read_text()) is None


def test_the_gate_and_the_standins_run_serve_and_stay_alive(
    env: ModuleType, tmp_path: Path
) -> None:
    unit_dir = install_plists(env, tmp_path)

    for service in ("gate", "standins"):
        plist = load_plist(unit_dir / f"glovebox.test.{service}.plist")
        assert plist["ProgramArguments"][2:] == ["serve", service, "--tier", "test"]
        assert plist["KeepAlive"] is True
        assert plist["RunAtLoad"] is True


def test_the_timer_runs_ensure_test_every_five_minutes_without_a_git_prompt(
    env: ModuleType, tmp_path: Path
) -> None:
    unit_dir = install_plists(env, tmp_path)

    plist = load_plist(unit_dir / "glovebox.test.ensure-test.plist")

    assert plist["ProgramArguments"][2:] == ["ensure-test", "--tier", "test"]
    assert plist["StartInterval"] == 300
    assert plist["EnvironmentVariables"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "KeepAlive" not in plist


def test_no_test_plist_needs_a_login_session_or_asks_for_one(
    env: ModuleType, tmp_path: Path
) -> None:
    unit_dir = install_plists(env, tmp_path)

    for name in TEST_PLISTS:
        plist = load_plist(unit_dir / name)
        assert "LimitLoadToSessionType" not in plist
        assert plist["UserName"]  # a daemon of that user, not an agent in a GUI session


def test_a_path_with_xml_characters_is_escaped_in_the_plist(
    env: ModuleType, tmp_path: Path
) -> None:
    unit_dir = install_plists(env, tmp_path, user_home=tmp_path / "a&b <home>")

    plist = load_plist(unit_dir / "glovebox.test.gate.plist")

    assert plist["EnvironmentVariables"]["HOME"] == str(tmp_path / "a&b <home>")


def test_installing_the_plists_twice_gives_the_same_files(env: ModuleType, tmp_path: Path) -> None:
    unit_dir = install_plists(env, tmp_path)
    first = sorted((p.name, p.read_text()) for p in unit_dir.iterdir())

    install_plists(env, tmp_path)

    assert sorted((p.name, p.read_text()) for p in unit_dir.iterdir()) == first
    assert [name for name, _ in first] == sorted(TEST_PLISTS)


def test_install_timer_will_not_bake_in_a_python_that_is_too_old(
    env: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(env.sys, "version_info", (3, 9, 6, "final", 0))

    code = env.main(["install-timer", "--no-enable", "--unit-dir", str(tmp_path / "units")])

    assert code == 1
    assert not (tmp_path / "units").exists()
    assert "python 3.12 or newer" in capsys.readouterr().out

    # An explicit --python is the operator's word for another interpreter.
    setattr(env, "user_home", lambda user: tmp_path)  # noqa: B010 - env is a loaded module
    args = ["install-timer", "--no-enable", "--unit-dir", str(tmp_path / "units")]
    assert env.main([*args, "--python", "/opt/homebrew/bin/python3"]) == 0


def test_install_timer_on_macos_will_not_enable_anything(
    env: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = env.main(["install-timer", "--unit-dir", str(tmp_path / "units")])

    assert code == 2
    assert not (tmp_path / "units").exists()
    assert "installed by root" in capsys.readouterr().out


@pytest.mark.parametrize("name", [*TEST_PLISTS, "glovebox.prod.gate.plist"])
def test_every_plist_is_wellformed_and_passes_plutil_where_it_exists(
    env: ModuleType, tmp_path: Path, name: str
) -> None:
    unit_dir = install_plists(env, tmp_path)
    rendered = unit_dir / name
    if name not in TEST_PLISTS:
        text = (LAUNCHD / name).read_text()
        for placeholder, value in {"SECRETS": "/s/gate.env", "REPO": "/r", "HOME": "/h"}.items():
            text = text.replace(f"@{placeholder}@", value)
        rendered = tmp_path / name
        rendered.write_text(text)

    ElementTree.parse(rendered)  # raises on a malformed document
    load_plist(rendered)
    plutil = shutil.which("plutil")
    if plutil is not None:
        result = subprocess.run([plutil, "-lint", str(rendered)], capture_output=True, check=False)
        assert result.returncode == 0


def test_the_production_plist_sources_a_secrets_file_and_holds_no_secret(tmp_path: Path) -> None:
    text = (LAUNCHD / "glovebox.prod.gate.plist").read_text()
    for placeholder, value in {"SECRETS": "/s/gate.env", "REPO": "/r", "HOME": "/h"}.items():
        text = text.replace(f"@{placeholder}@", value)

    plist = plistlib.loads(text.encode())

    assert re.search(r"@[A-Z]+@", text) is None  # these three are all it takes
    assert plist["Label"] == "glovebox.prod.gate"
    assert "UserName" not in plist  # an agent in frogger's own session
    assert "EnvironmentVariables" not in plist
    assert plist["ProgramArguments"][-2:] == ["/s/gate.env", "/r/.venv/bin/glovebox"]
    assert plist["StandardOutPath"].startswith("/h/Library/Logs/")


def runbook_position(runbook: str, text: str) -> int:
    position = runbook.find(text)
    assert position >= 0, text
    return position


def test_the_runbook_stops_a_loaded_gate_before_it_changes_anything_and_before_bootstrapping() -> (
    None
):
    doc = (ROOT / "docs" / "environments.md").read_text()
    runbook = doc[doc.index("## Production runbook") :]
    stop = runbook_position(runbook, "launchctl bootout gui/$(id -u)/glovebox.prod.gate")

    # A second deployment finds the first one loaded: `bootstrap` would fail or leave it running.
    assert stop < runbook_position(runbook, "git -C ~/glovebox checkout")
    assert stop < runbook_position(runbook, ".venv/bin/pip install")
    assert stop < runbook_position(runbook, "> ~/Library/LaunchAgents/")
    assert stop < runbook_position(runbook, "launchctl bootstrap gui/$(id -u)")
    assert "|| true" in runbook[stop : stop + 120]  # the first deployment has nothing to stop
    assert stop < runbook_position(runbook, "scripts/env.py check --url")


def test_the_runbooks_plist_step_works_from_a_fresh_home(tmp_path: Path) -> None:
    """Run the plist step's shell block as written, with a HOME that has no Library directory."""
    doc = (ROOT / "docs" / "environments.md").read_text()
    found = re.search(r"```sh\n(\s+mkdir -p ~/Library/LaunchAgents.*?)```", doc, re.S)
    assert found is not None
    block = textwrap.dedent(found.group(1))
    home = tmp_path / "frogger"
    home.mkdir()
    (home / "glovebox").symlink_to(ROOT)  # the runbook clones the repository to ~/glovebox
    lines = [line for line in block.splitlines() if "plutil" not in line]  # not on Linux

    result = subprocess.run(
        ["sh", "-eu", "-c", "\n".join(lines)],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    agent = home / "Library" / "LaunchAgents" / "glovebox.prod.gate.plist"
    plist = load_plist(agent)
    assert plist["ProgramArguments"][-1] == f"{home}/glovebox/.venv/bin/glovebox"
    assert Path(plist["StandardOutPath"]).parent == home / "Library" / "Logs" / "glovebox"
    assert Path(plist["StandardOutPath"]).parent.is_dir()  # launchd does not create it


# --- the command plan -------------------------------------------------------------------


def test_the_gate_runs_from_the_venv_on_loopback_with_the_token_and_standin_addresses(
    env: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("GLOVEBOX_TEST_TOKEN=abc123\n")

    command, environment = env.service_plan("gate")

    # `--bootstrap-fixture` registers the test token as an issued token for the fixture account.
    assert command == [str(tmp_path / "venv" / "bin" / "glovebox"), "serve", "--bootstrap-fixture"]
    assert environment["GLOVEBOX_HOST"] == "127.0.0.1"
    assert environment["GLOVEBOX_PORT"] == "8765"
    assert environment["GLOVEBOX_TEST_TOKEN"] == "abc123"
    assert environment["GLOVEBOX_FIXTURE_TOKEN"] == "abc123"
    assert "GLOVEBOX_AGENT_TOKEN" not in environment
    # Next to the seeded state, which `down` removes.
    assert environment["GLOVEBOX_DB"] == str(tmp_path / "run" / "state" / "glovebox.db")
    assert environment["GLOVEBOX_STANDIN_IMESSAGE"] == "http://127.0.0.1:9106"
    assert environment["GLOVEBOX_STANDIN_INFERENCE_EXTERNAL"] == "http://127.0.0.1:9109"


def test_the_gate_port_follows_the_publish_port_knob(
    env: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GLOVEBOX_ENV_PUBLISH_PORT", "18765")

    _, environment = env.service_plan("gate")

    assert environment["GLOVEBOX_PORT"] == "18765"


def test_the_gate_defaults_to_loopback_with_no_extra_allowed_hosts(
    env: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An ambient setting of the gate's own is not passed through: only the knobs count.
    monkeypatch.setenv("GLOVEBOX_ALLOWED_HOSTS", "*")

    _, environment = env.service_plan("gate")

    assert environment["GLOVEBOX_HOST"] == "127.0.0.1"
    assert "GLOVEBOX_ALLOWED_HOSTS" not in environment


def test_the_allowed_hosts_knob_reaches_the_gate_from_the_environment_or_the_env_file(
    env: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GLOVEBOX_ENV_ALLOWED_HOSTS", "a.ts.net:8765")
    _, from_environment = env.service_plan("gate")

    # The daemon's environment comes from its plist, so the env file is where the operator sets it.
    monkeypatch.delenv("GLOVEBOX_ENV_ALLOWED_HOSTS")
    (tmp_path / ".env").write_text("GLOVEBOX_ENV_ALLOWED_HOSTS=b.ts.net:8765, c.ts.net:8765\n")
    _, from_file = env.service_plan("gate")

    assert from_environment["GLOVEBOX_ALLOWED_HOSTS"] == "a.ts.net:8765"
    assert from_file["GLOVEBOX_ALLOWED_HOSTS"] == "b.ts.net:8765,c.ts.net:8765"
    assert from_file["GLOVEBOX_HOST"] == "127.0.0.1"


@pytest.mark.parametrize(
    "value",
    ["*", "*.ts.net", "host.ts.net:*", "a.ts.net,*", "a.ts.net,,b.ts.net", "https://a.ts.net:8765"],
)
def test_a_wildcard_or_malformed_allowed_host_is_rejected(
    env: ModuleType, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GLOVEBOX_ENV_ALLOWED_HOSTS", value)

    with pytest.raises(SystemExit, match="GLOVEBOX_ENV_ALLOWED_HOSTS"):
        env.service_plan("gate")


@pytest.mark.parametrize("value", ["a.ts.net:8765", "A.ts.net", "[::1]:8765", "a.ts.net:8765,b"])
def test_the_runner_accepts_what_the_gate_accepts(
    env: ModuleType, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GLOVEBOX_ENV_ALLOWED_HOSTS", value)
    _, environment = env.service_plan("gate")

    assert parse_allowed_hosts(environment["GLOVEBOX_ALLOWED_HOSTS"])


def test_the_runner_and_the_gate_share_one_host_pattern(env: ModuleType) -> None:
    assert env.HOST_PATTERN.pattern == HOST_PATTERN.pattern


@pytest.mark.parametrize(
    "value", ["*", "a.ts.net:*", "a.ts.net:99999", "[::1]:99999", "a.ts.net/mcp", "a b"]
)
def test_the_runner_refuses_what_the_gate_refuses_so_up_fails_at_once(
    env: ModuleType, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("GLOVEBOX_ENV_ALLOWED_HOSTS", value)

    with pytest.raises(ConfigError):
        parse_allowed_hosts(value)
    with pytest.raises(SystemExit):
        env.service_plan("gate")


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("127.0.0.1", None),
        ("::1", None),
        # The gate answers loopback by default, but only 127.0.0.1 and ::1 by name.
        ("127.0.0.2", "127.0.0.2:8765"),
        ("100.101.102.103", "100.101.102.103:8765"),
        ("fd7a:115c:a1e0::1", "[fd7a:115c:a1e0::1]:8765"),
    ],
)
def test_the_publish_host_knob_moves_the_gate_to_a_loopback_or_tailnet_address(
    env: ModuleType, monkeypatch: pytest.MonkeyPatch, address: str, allowed: str | None
) -> None:
    monkeypatch.setenv("GLOVEBOX_ENV_PUBLISH_HOST", address)

    _, environment = env.service_plan("gate")

    assert environment["GLOVEBOX_HOST"] == address
    # A gate bound off 127.0.0.1 must answer its own address, so that `check` can probe it.
    assert environment.get("GLOVEBOX_ALLOWED_HOSTS") == allowed


def test_the_published_address_joins_the_configured_hosts_without_repeating_one(
    env: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GLOVEBOX_ENV_PUBLISH_HOST", "100.101.102.103")
    monkeypatch.setenv("GLOVEBOX_ENV_PUBLISH_PORT", "18765")
    monkeypatch.setenv("GLOVEBOX_ENV_ALLOWED_HOSTS", "a.ts.net:18765,100.101.102.103:18765")

    _, environment = env.service_plan("gate")

    assert environment["GLOVEBOX_ALLOWED_HOSTS"] == "a.ts.net:18765,100.101.102.103:18765"


@pytest.mark.parametrize(
    "address",
    ["", "0.0.0.0", "::", "192.168.1.5", "8.8.8.8", "10.0.0.1", "localhost", "a.ts.net", "[::1]"],
)
def test_a_wildcard_or_public_publish_host_is_rejected(
    env: ModuleType, monkeypatch: pytest.MonkeyPatch, address: str
) -> None:
    monkeypatch.setenv("GLOVEBOX_ENV_PUBLISH_HOST", address)

    with pytest.raises(SystemExit, match="GLOVEBOX_ENV_PUBLISH_HOST"):
        env.service_plan("gate")


def test_the_stand_ins_stay_on_loopback_whatever_the_gate_publishes(
    env: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GLOVEBOX_ENV_PUBLISH_HOST", "100.101.102.103")
    monkeypatch.setenv("GLOVEBOX_ENV_ALLOWED_HOSTS", "a.ts.net:8765")

    _, standins = env.service_plan("standins")
    _, gate = env.service_plan("gate")

    assert standins["STANDINS_HOST"] == "127.0.0.1"
    assert "GLOVEBOX_ALLOWED_HOSTS" not in standins
    assert gate["GLOVEBOX_STANDIN_MAILBOX_MAIL"] == "http://127.0.0.1:9101"


def test_the_local_check_probes_the_address_the_gate_is_published_on(
    env: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    probed = []

    def record(url: str, token: str | None) -> str:
        probed.append(url)
        return "ok"

    monkeypatch.setattr(env, "probe_gate", record)
    monkeypatch.setattr(env, "native_standins_status", lambda: "ok")

    env.native_status()
    monkeypatch.setenv("GLOVEBOX_ENV_PUBLISH_HOST", "100.101.102.103")
    monkeypatch.setenv("GLOVEBOX_ENV_PUBLISH_PORT", "18765")
    env.native_status()
    monkeypatch.setenv("GLOVEBOX_ENV_PUBLISH_HOST", "::1")
    env.native_status()

    assert probed == [
        "http://127.0.0.1:8765/mcp",
        "http://100.101.102.103:18765/mcp",
        "http://[::1]:18765/mcp",
    ]


def test_the_standins_run_from_the_checkout_on_loopback_over_the_seeded_state(
    env: ModuleType, tmp_path: Path
) -> None:
    command, environment = env.service_plan("standins")

    assert command == [str(tmp_path / "venv" / "bin" / "python"), str(STANDINS)]
    assert environment["STANDINS_HOST"] == "127.0.0.1"
    assert environment["STANDINS_STATE"] == str(tmp_path / "run" / "state" / "seed")


def test_the_standin_variables_match_compose_yaml() -> None:
    env = load_env_module()
    compose = (ROOT / "compose.yaml").read_text()

    declared = {
        name: int(port)
        for name, port in re.findall(r"(GLOVEBOX_STANDIN_\w+): http://standins:(\d+)", compose)
    }

    assert declared == env.STANDIN_VARS


def test_the_standin_ports_match_the_listeners() -> None:
    env = load_env_module()
    standins = load_module("glovebox_standins_ports", STANDINS)

    assert sorted(env.STANDIN_VARS.values()) == sorted(standins.SERVICES.values())


def test_the_standins_bind_the_host_they_are_told_to(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    standins = load_module("glovebox_standins_host", STANDINS)
    hosts: list[str] = []

    class Quiet:
        def serve_forever(self) -> None:
            pass

    def fake_server(service: str, state: Path, host: str, port: int) -> Quiet:
        hosts.append(host)
        return Quiet()

    monkeypatch.setattr(standins, "make_server", fake_server)
    monkeypatch.delenv("STANDINS_HOST", raising=False)
    standins.main()
    assert set(hosts) == {"0.0.0.0"}  # the container default is unchanged

    hosts.clear()
    monkeypatch.setenv("STANDINS_HOST", "127.0.0.1")
    standins.main()
    assert set(hosts) == {"127.0.0.1"}


def test_only_the_test_tier_uses_the_native_backend(
    env: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert env.backend("test") == "native"
    assert env.backend("throwaway") == "docker"

    monkeypatch.setenv("GLOVEBOX_ENV_BACKEND", "docker")
    assert env.backend("test") == "docker"

    monkeypatch.delenv("GLOVEBOX_ENV_BACKEND")
    monkeypatch.setattr(env.sys, "platform", "darwin")
    assert env.backend("test") == "native"
    assert env.backend("throwaway") == "docker"
    monkeypatch.setattr(env.sys, "platform", "linux")
    assert env.backend("test") == "docker"


def test_an_unknown_backend_is_rejected(env: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLOVEBOX_ENV_BACKEND", "podman")

    with pytest.raises(SystemExit, match="unknown backend"):
        env.backend("test")


def test_a_bad_publish_port_is_rejected(env: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GLOVEBOX_ENV_PUBLISH_PORT", "eighty")

    with pytest.raises(SystemExit, match="must be a port number"):
        env.service_plan("gate")


def test_serve_refuses_to_run_on_the_docker_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "env.py"), "serve", "gate", "--tier", "test"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={"PATH": "/usr/bin:/bin", "GLOVEBOX_ENV_BACKEND": "docker"},
    )

    assert result.returncode == 2
    assert result.stdout == "serve: runs on the native test backend only\n"


# --- up, reset, down and check ----------------------------------------------------------


class Recorder:
    """Stands in for subprocess.run and records every command the backend runs."""

    def __init__(self, returncode: int = 0) -> None:
        self.commands: list[list[str]] = []
        self.returncode = returncode

    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        return subprocess.CompletedProcess(command, self.returncode, "", "")


def test_up_installs_seeds_and_asks_for_the_release_it_ran(
    env: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    def install() -> bool:
        calls.append("install")
        return True

    def seed() -> bool:
        calls.append("seed")
        return True

    monkeypatch.setattr(env, "install_dependencies", install)
    monkeypatch.setattr(env, "native_seed", seed)
    monkeypatch.setattr(env, "native_status", lambda: ("ok", "ok"))
    monkeypatch.setattr(env, "release_fingerprint", lambda: "abc-123")
    monkeypatch.setattr(env, "running_release", lambda service: "up abc-123")

    assert env.main(["up"]) == 0

    assert calls == ["install", "seed"]
    assert env.read_desired() == "up abc-123"
    assert "created .env" in capsys.readouterr().out
    assert env.read_env_file()["GLOVEBOX_TEST_TOKEN"]


def test_the_release_changes_with_the_gate_settings_the_runner_reads(
    env: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("GLOVEBOX_TEST_TOKEN=abc123\n")
    before = env.release_fingerprint()
    assert env.release_fingerprint() == before  # nothing changed: the same release

    (tmp_path / ".env").write_text(
        "GLOVEBOX_TEST_TOKEN=abc123\nGLOVEBOX_ENV_ALLOWED_HOSTS=a.ts.net:8765\n"
    )
    allowed = env.release_fingerprint()
    (tmp_path / ".env").write_text(
        "GLOVEBOX_TEST_TOKEN=abc123\nGLOVEBOX_ENV_ALLOWED_HOSTS=b.ts.net:8765\n"
    )
    other_host = env.release_fingerprint()
    monkeypatch.setenv("GLOVEBOX_ENV_PUBLISH_HOST", "100.101.102.103")
    published = env.release_fingerprint()
    monkeypatch.delenv("GLOVEBOX_ENV_PUBLISH_HOST")
    monkeypatch.setenv("GLOVEBOX_ENV_PUBLISH_PORT", "18765")
    other_port = env.release_fingerprint()

    assert len({before, allowed, other_host, published, other_port}) == 5


def test_the_release_changes_with_any_setting_in_the_env_file_such_as_a_rotated_token(
    env: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text("GLOVEBOX_TEST_TOKEN=abc123\n")
    before = env.release_fingerprint()

    (tmp_path / ".env").write_text("GLOVEBOX_TEST_TOKEN=def456\n")
    rotated = env.release_fingerprint()
    (tmp_path / ".env").write_text(
        "GLOVEBOX_TEST_TOKEN=def456\nGLOVEBOX_FIXED_NOW=2026-01-10T12:00Z\n"
    )
    pinned = env.release_fingerprint()

    assert len({before, rotated, pinned}) == 3


def test_the_release_does_not_carry_the_settings_in_the_clear(
    env: ModuleType, tmp_path: Path
) -> None:
    (tmp_path / ".env").write_text(
        "GLOVEBOX_TEST_TOKEN=abc123xyz\nGLOVEBOX_ENV_ALLOWED_HOSTS=florians.ts.net:8765\n"
    )

    fingerprint = env.release_fingerprint()

    assert "florians" not in fingerprint
    assert "abc123xyz" not in fingerprint


def test_up_asks_for_a_new_release_when_the_allowed_hosts_in_the_env_file_change(
    env: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env, "install_dependencies", lambda: True)
    monkeypatch.setattr(env, "native_seed", lambda: True)
    monkeypatch.setattr(env, "native_status", lambda: ("ok", "ok"))
    monkeypatch.setattr(env, "running_release", lambda service: env.read_desired())
    (tmp_path / ".env").write_text("GLOVEBOX_TEST_TOKEN=abc123\n")
    assert env.main(["up"]) == 0
    first = env.read_desired()
    assert env.main(["up"]) == 0
    assert env.read_desired() == first  # the same settings: the same release, nothing restarts

    with (tmp_path / ".env").open("a") as env_file:
        env_file.write("GLOVEBOX_ENV_ALLOWED_HOSTS=florians-mac-mini.tail974337.ts.net:8765\n")
    assert env.main(["up"]) == 0

    assert env.read_desired() != first


def test_a_changed_allowed_host_reaches_the_running_gate_on_the_next_up(
    env: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole path: `.env`, then `up`, then `serve` restarts the gate with the new setting."""
    seen = tmp_path / "gate-allowed-hosts"
    real_plan = env.service_plan

    def plan(service: str) -> tuple[list[str], dict[str, str]]:
        _, environment = real_plan(service)
        # A stand-in gate: it records the allowed hosts it was started with, then sleeps.
        record = textwrap.dedent(
            f"""\
            import os, pathlib, time
            hosts = os.environ.get('GLOVEBOX_ALLOWED_HOSTS', '-')
            pathlib.Path({str(seen)!r}).write_text(hosts)
            time.sleep(60)
            """
        )
        return [sys.executable, "-c", record], environment

    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(env, "install_dependencies", lambda: True)
    monkeypatch.setattr(env, "native_seed", lambda: True)
    monkeypatch.setattr(env, "native_status", lambda: ("ok", "ok"))
    monkeypatch.setattr(env, "running_release", lambda service: env.read_desired())
    monkeypatch.setattr(env, "service_plan", plan)
    monkeypatch.setattr(env, "RESTART_DELAY_SECONDS", 0)
    handlers = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    (tmp_path / ".env").write_text("GLOVEBOX_TEST_TOKEN=abc123\n")

    failures: list[BaseException] = []

    def script() -> None:
        try:
            assert env.main(["up"]) == 0
            # Loopback only: the gate is told of no extra host.
            wait_for(lambda: seen.exists() and seen.read_text() == "-")
            first = running_pid(env)
            with (tmp_path / ".env").open("a") as env_file:
                env_file.write(
                    "GLOVEBOX_ENV_ALLOWED_HOSTS=florians-mac-mini.tail974337.ts.net:8765\n"
                )
            assert env.main(["up"]) == 0

            def restarted() -> bool:
                try:
                    return running_pid(env) != first
                except FileNotFoundError:  # `serve` removes it between the old gate and the new
                    return False

            wait_for(restarted)
            wait_for(lambda: seen.read_text() == "florians-mac-mini.tail974337.ts.net:8765")
        except BaseException as failure:  # the driver runs in a thread: report it from here
            failures.append(failure)

    try:
        assert run_serve(env, script) == 0
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)
    assert failures == []


def test_up_reports_a_start_that_never_became_healthy(
    env: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(env, "install_dependencies", lambda: True)
    monkeypatch.setattr(env, "native_seed", lambda: True)
    monkeypatch.setattr(env, "native_status", lambda: ("unreachable", "missing"))
    monkeypatch.setattr(env, "NATIVE_WAIT_SECONDS", 0)

    assert env.main(["up"]) == 1

    last = capsys.readouterr().out.splitlines()[-1]
    assert last == "start: failed, no serve process is running"


def test_up_does_not_accept_the_previous_release_answering_healthy(
    env: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(env, "install_dependencies", lambda: True)
    monkeypatch.setattr(env, "native_seed", lambda: True)
    monkeypatch.setattr(env, "native_status", lambda: ("ok", "ok"))
    monkeypatch.setattr(env, "release_fingerprint", lambda: "new")
    monkeypatch.setattr(env, "running_release", lambda service: "up old")
    monkeypatch.setattr(env, "pid_alive", lambda path: True)
    monkeypatch.setattr(env, "NATIVE_WAIT_SECONDS", 0)

    assert env.main(["up"]) == 1

    assert capsys.readouterr().out.splitlines()[-1] == "start: failed"


def test_up_stops_at_a_failed_install(
    env: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(env, "install_dependencies", lambda: False)

    assert env.main(["up"]) == 1

    assert env.read_desired() == ""
    assert "up: ok" not in capsys.readouterr().out


def test_up_refuses_a_wildcard_publish_host_before_it_installs_or_asks_for_anything(
    env: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    monkeypatch.setattr(env, "run_command", recorder)
    monkeypatch.setattr(env, "install_dependencies", lambda: pytest.fail("installed"))
    monkeypatch.setenv("GLOVEBOX_ENV_PUBLISH_HOST", "0.0.0.0")

    with pytest.raises(SystemExit, match="GLOVEBOX_ENV_PUBLISH_HOST"):
        env.main(["up"])

    assert recorder.commands == []
    assert env.read_desired() == ""


def test_dependencies_install_once_per_set_of_pins(
    env: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    monkeypatch.setattr(env.subprocess, "run", recorder)
    python = tmp_path / "venv" / "bin" / "python"

    def make_venv(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[1:3] == ["-m", "venv"]:
            python.parent.mkdir(parents=True)
            python.touch()
        return recorder(command, **kwargs)

    monkeypatch.setattr(env.subprocess, "run", make_venv)

    assert env.install_dependencies() is True
    assert [c[1:3] for c in recorder.commands] == [["-m", "venv"], ["-m", "pip"]]
    assert recorder.commands[1][-2:] == ["-e", "."]
    assert "constraints.txt" in recorder.commands[1]

    recorder.commands.clear()
    assert env.install_dependencies() is True
    assert recorder.commands == []  # the stamp matches: nothing to do

    (tmp_path / "venv" / ".glovebox-install").write_text("an older set of pins")
    assert env.install_dependencies() is True
    assert [c[1:3] for c in recorder.commands] == [["-m", "pip"]]


def test_a_failed_pip_install_leaves_no_stamp(
    env: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").touch()
    monkeypatch.setattr(env.subprocess, "run", Recorder(returncode=1))

    assert env.install_dependencies() is False
    assert not (tmp_path / "venv" / ".glovebox-install").exists()


def test_reset_reseeds_the_state_directory_the_standins_read(
    env: ModuleType, tmp_path: Path
) -> None:
    assert env.main(["reset"]) == 0
    state = tmp_path / "run" / "state" / "seed"
    first = {p.name: p.read_bytes() for p in state.glob("*.json")}

    assert env.main(["reset"]) == 0

    assert "manifest.json" in first and "imessage.json" in first
    assert {p.name: p.read_bytes() for p in state.glob("*.json")} == first


def test_down_stops_asking_for_the_services_and_removes_the_seeded_state(
    env: ModuleType, tmp_path: Path
) -> None:
    env.main(["reset"])
    env.write_desired("up abc")

    assert env.main(["down"]) == 0

    assert env.read_desired() == "down"
    assert not (tmp_path / "run" / "state").exists()


def test_down_waits_for_the_supervised_processes_to_stop(
    env: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(env, "STOP_GRACE_SECONDS", 0.01)
    (tmp_path / "run").mkdir()
    sleeper = subprocess.Popen(SLEEPER)
    try:
        env.pid_file("gate").write_text(f"{sleeper.pid}\n")

        assert env.main(["down"]) == 1  # nothing stopped it
    finally:
        sleeper.kill()
        sleeper.wait()
    assert env.main(["down"]) == 0


def test_the_native_backend_never_runs_launchctl_or_sudo(
    env: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = Recorder()
    monkeypatch.setattr(env.subprocess, "run", recorder)
    monkeypatch.setattr(env, "native_status", lambda: ("ok", "ok"))
    monkeypatch.setattr(env, "running_release", lambda service: env.read_desired())
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").touch()
    (tmp_path / "venv" / ".glovebox-install").write_text(env.dependency_digest())

    env.main(["up"])
    env.main(["reset"])
    env.main(["down"])
    unit_dir = install_plists(env, tmp_path)
    assert unit_dir.is_dir()

    words = {part for command in recorder.commands for part in command}
    assert recorder.commands  # it did run the seed
    assert not words & {"launchctl", "sudo", "docker"}


@pytest.mark.parametrize(
    ("status", "code", "lines"),
    [
        (("ok", "ok"), 0, ["standins: ok", "gate: ok", "check: healthy"]),
        (
            ("unreachable", "missing"),
            1,
            ["standins: missing", "gate: unreachable", "check: unhealthy"],
        ),
        (("ok", "unhealthy"), 1, ["standins: unhealthy", "gate: ok", "check: unhealthy"]),
        (("unhealthy", "ok"), 1, ["standins: ok", "gate: unhealthy", "check: unhealthy"]),
    ],
)
def test_check_reports_the_native_status_words(
    env: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: tuple[str, str],
    code: int,
    lines: list[str],
) -> None:
    monkeypatch.setattr(env, "native_status", lambda: status)

    assert env.main(["check"]) == code
    assert capsys.readouterr().out.splitlines() == lines


def test_the_standins_are_ok_only_when_every_one_is(
    env: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    outcome = {"value": "ok"}
    monkeypatch.setattr(env, "probe_health", lambda port: outcome["value"])

    assert env.native_standins_status() == "ok"
    outcome["value"] = "unreachable"
    assert env.native_standins_status() == "missing"
    outcome["value"] = "unhealthy"
    assert env.native_standins_status() == "unhealthy"
    monkeypatch.setattr(env, "probe_health", lambda port: "ok" if port == 9101 else "unreachable")
    assert env.native_standins_status() == "unhealthy"


def test_a_standin_health_probe_tells_ok_unhealthy_and_unreachable_apart(
    env: ModuleType,
) -> None:
    status = {"code": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(status["code"])
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    port = server.server_address[1]
    try:
        assert env.probe_health(port) == "ok"
        status["code"] = 503
        assert env.probe_health(port) == "unhealthy"
    finally:
        server.shutdown()
        thread.join()
        server.server_close()
    assert env.probe_health(port) == "unreachable"


# --- serve ------------------------------------------------------------------------------


@pytest.fixture
def serving(
    env: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[ModuleType]:
    """`serve` on a fake service, a sleeping process; the signal handlers it sets are undone."""
    python = tmp_path / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.touch()
    monkeypatch.setattr(env, "service_plan", lambda service: (SLEEPER, {}))
    monkeypatch.setattr(env, "RESTART_DELAY_SECONDS", 0)
    handlers = {number: signal.getsignal(number) for number in (signal.SIGTERM, signal.SIGINT)}
    yield env
    for number, handler in handlers.items():
        signal.signal(number, handler)


def run_serve(env: ModuleType, script: Any) -> int:
    """Run `serve gate` in this thread while `script` drives it from another; SIGTERM ends it."""

    def drive() -> None:
        try:
            script()
        finally:
            signal.raise_signal(signal.SIGTERM)

    driver = threading.Thread(target=drive)
    driver.start()
    try:
        return int(env.serve("gate", ["env.py"], poll=0.02))
    finally:
        driver.join()


def wait_for(condition: Any, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.02)


def running_pid(env: ModuleType) -> int:
    return int(env.pid_file("gate").read_text().split()[0])


def test_serve_runs_the_service_while_up_is_wanted_and_stops_it_on_down(
    serving: ModuleType,
) -> None:
    env = serving
    seen: dict[str, int] = {}

    def script() -> None:
        assert env.read_desired() == ""
        time.sleep(0.2)
        assert not env.pid_file("gate").exists()  # nothing wanted: nothing runs
        env.write_desired("up one")
        wait_for(lambda: env.pid_file("gate").exists())
        seen["pid"] = running_pid(env)
        assert env.pid_alive(env.pid_file("gate"))
        env.write_desired("down")
        wait_for(lambda: not env.pid_file("gate").exists())

    assert run_serve(env, script) == 0

    with pytest.raises(ProcessLookupError):
        os.kill(seen["pid"], 0)


def test_serve_restarts_the_service_when_the_wanted_release_changes_only(
    serving: ModuleType,
) -> None:
    env = serving
    pids: list[int] = []

    def script() -> None:
        env.write_desired("up one")
        wait_for(lambda: env.pid_file("gate").exists())
        pids.append(running_pid(env))
        env.write_desired("up one")  # `up` again: the same release
        time.sleep(0.3)
        pids.append(running_pid(env))
        env.write_desired("up two")
        wait_for(lambda: running_pid(env) != pids[0])
        pids.append(running_pid(env))

    run_serve(env, script)

    assert pids[0] == pids[1] != pids[2]


def test_serve_restarts_a_service_that_died(serving: ModuleType) -> None:
    env = serving
    pids: list[int] = []

    def script() -> None:
        env.write_desired("up one")
        wait_for(lambda: env.pid_file("gate").exists())
        pids.append(running_pid(env))
        os.kill(pids[0], signal.SIGKILL)
        wait_for(lambda: env.pid_file("gate").exists() and running_pid(env) != pids[0])
        pids.append(running_pid(env))

    run_serve(env, script)

    assert pids[0] != pids[1]


def test_serve_waits_before_restarting_a_service_that_keeps_dying(
    serving: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = serving
    monkeypatch.setattr(env, "RESTART_DELAY_SECONDS", 30)
    starts: list[int] = []

    def script() -> None:
        env.write_desired("up one")
        wait_for(lambda: env.pid_file("gate").exists())
        starts.append(running_pid(env))
        os.kill(starts[0], signal.SIGKILL)
        time.sleep(0.5)  # many polls
        assert not env.pid_file("gate").exists()

    run_serve(env, script)


def test_the_running_release_is_empty_when_the_pid_file_goes_away_while_it_is_read(
    env: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `serve` removes the old pid file during a restart, between our two looks at it.
    (tmp_path / "run").mkdir()
    path = env.pid_file("gate")
    path.write_text(f"{os.getpid()} up one\n")
    real_alive = env.pid_alive

    def alive_then_gone(candidate: Path) -> bool:
        alive: bool = real_alive(candidate)
        candidate.unlink()
        return alive

    monkeypatch.setattr(env, "pid_alive", alive_then_gone)

    assert env.running_release("gate") == ""


def test_up_waits_through_a_restart_that_removes_the_pid_file(
    env: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "run").mkdir()
    monkeypatch.setattr(env, "install_dependencies", lambda: True)
    monkeypatch.setattr(env, "native_seed", lambda: True)
    monkeypatch.setattr(env, "native_status", lambda: ("ok", "ok"))
    monkeypatch.setattr(env, "release_fingerprint", lambda: "new")
    monkeypatch.setattr(env.time, "sleep", lambda seconds: None)
    looks = {"count": 0}

    def old_then_gone_then_new(service: str) -> str:
        looks["count"] += 1
        return ("up old", "", "up new")[min(looks["count"] // 2, 2)]

    monkeypatch.setattr(env, "running_release", old_then_gone_then_new)

    assert env.main(["up"]) == 0


def test_serve_writes_the_pid_file_whole_or_not_at_all(serving: ModuleType, tmp_path: Path) -> None:
    env = serving
    reads: list[str] = []

    def script() -> None:
        env.write_desired("up one")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                reads.append(env.pid_file("gate").read_text())
            except FileNotFoundError:
                pass

    run_serve(env, script)

    assert reads
    assert all(text.endswith(" up one\n") for text in reads)
    assert [path.name for path in (tmp_path / "run").glob("gate.pid*")] == []


def test_serve_records_the_release_each_process_runs_and_clears_a_stale_pid_file(
    serving: ModuleType, tmp_path: Path
) -> None:
    env = serving
    (tmp_path / "run").mkdir()
    env.pid_file("gate").write_text("1 up leftover\n")
    seen: dict[str, str] = {}

    def script() -> None:
        wait_for(lambda: not env.pid_file("gate").exists())  # serve drops the leftover
        env.write_desired("up one")
        wait_for(lambda: env.running_release("gate") == "up one")
        seen["release"] = env.running_release("gate")

    run_serve(env, script)

    assert seen == {"release": "up one"}
    assert env.running_release("gate") == ""


def test_serve_survives_env_py_vanishing_for_a_moment(
    serving: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = serving
    script_file = tmp_path / "env.py"
    script_file.write_text("# during a git checkout")
    monkeypatch.setattr(env, "SCRIPT", script_file)

    def script() -> None:
        env.write_desired("up one")
        wait_for(lambda: env.pid_file("gate").exists())
        script_file.unlink()
        time.sleep(0.3)
        assert env.pid_file("gate").exists()  # still running, not crashed

    assert run_serve(env, script) == 0


def test_serve_stops_the_service_when_launchd_stops_it(serving: ModuleType) -> None:
    env = serving
    pids: list[int] = []

    def script() -> None:
        env.write_desired("up one")
        wait_for(lambda: env.pid_file("gate").exists())
        pids.append(running_pid(env))

    assert run_serve(env, script) == 0

    assert not env.pid_file("gate").exists()
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)


def test_serve_waits_for_the_venv_before_starting(
    serving: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = serving
    monkeypatch.setattr(env, "VENV", tmp_path / "no-venv-yet")

    def script() -> None:
        env.write_desired("up one")
        time.sleep(0.3)
        assert not env.pid_file("gate").exists()

    run_serve(env, script)


class Reexecuted(Exception):
    pass


def test_serve_reexecutes_a_changed_env_py_after_stopping_the_service(
    serving: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = serving
    script_file = tmp_path / "env.py"
    script_file.write_text("# old")
    monkeypatch.setattr(env, "SCRIPT", script_file)
    argvs: list[list[str]] = []

    def reexec(argv: list[str]) -> None:
        argvs.append(argv)
        raise Reexecuted

    monkeypatch.setattr(env, "reexec", reexec)
    pids: list[int] = []

    def drive() -> None:
        env.write_desired("up one")
        wait_for(lambda: env.pid_file("gate").exists())
        pids.append(running_pid(env))
        script_file.write_text("# new")
        later = time.time() + 5
        os.utime(script_file, (later, later))

    driver = threading.Thread(target=drive)
    driver.start()
    try:
        with pytest.raises(Reexecuted):
            env.serve("gate", ["env.py", "serve", "gate"], poll=0.02)
    finally:
        driver.join()

    assert argvs == [["env.py", "serve", "gate"]]
    assert not env.pid_file("gate").exists()  # the service stopped before the new script took over
    with pytest.raises(ProcessLookupError):
        os.kill(pids[0], 0)
