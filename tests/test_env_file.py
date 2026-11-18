"""`scripts/env.py up` owns the gitignored env file; tier and timer behavior (ADR 0007)."""

import importlib.util
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENV_SCRIPT = ROOT / "scripts" / "env.py"


def load_env_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("glovebox_env_script", ENV_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_a_missing_env_file_is_created_with_a_fresh_private_token(tmp_path: Path) -> None:
    env = load_env_module()
    path = tmp_path / ".env"

    assert env.ensure_env_file(path) is True

    name, _, token = path.read_text().strip().partition("=")
    assert name == "GLOVEBOX_TEST_TOKEN"
    assert len(token) >= 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_creating_the_env_file_leaves_nothing_else_behind(tmp_path: Path) -> None:
    env = load_env_module()

    env.ensure_env_file(tmp_path / ".env")

    assert [path.name for path in tmp_path.iterdir()] == [".env"]


def test_the_local_token_comes_from_the_environment_first_then_the_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = load_env_module()
    monkeypatch.setattr(env, "ENV_FILE", tmp_path / ".env")
    monkeypatch.delenv("GLOVEBOX_TEST_TOKEN", raising=False)
    assert env.read_token("GLOVEBOX_TEST_TOKEN") is None

    (tmp_path / ".env").write_text("GLOVEBOX_TEST_TOKEN=from-the-file\n")
    assert env.read_token("GLOVEBOX_TEST_TOKEN") == "from-the-file"

    monkeypatch.setenv("GLOVEBOX_TEST_TOKEN", "from-the-environment")
    assert env.read_token("GLOVEBOX_TEST_TOKEN") == "from-the-environment"


def test_an_existing_env_file_is_left_alone(tmp_path: Path) -> None:
    env = load_env_module()
    path = tmp_path / ".env"
    path.write_text("GLOVEBOX_TEST_TOKEN=chosen-by-the-operator\n")

    assert env.ensure_env_file(path) is False

    assert path.read_text() == "GLOVEBOX_TEST_TOKEN=chosen-by-the-operator\n"


def test_two_new_env_files_get_different_tokens(tmp_path: Path) -> None:
    env = load_env_module()
    env.ensure_env_file(tmp_path / "a")
    env.ensure_env_file(tmp_path / "b")

    assert (tmp_path / "a").read_text() != (tmp_path / "b").read_text()


@pytest.mark.parametrize("name", [".env", ".env.lock"])
def test_env_files_are_gitignored(name: str) -> None:
    result = subprocess.run(["git", "check-ignore", "--quiet", name], cwd=ROOT, check=False)

    assert result.returncode == 0


def run_env(*args: str, tier: str = "throwaway") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ENV_SCRIPT), *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        env={**os.environ, "GLOVEBOX_ENV_TIER": tier},
    )


def test_ensure_test_refuses_to_move_a_throwaway_checkout() -> None:
    result = run_env("ensure-test")

    assert result.returncode == 2
    assert result.stdout == "ensure-test: runs on the test tier only\n"


def test_install_timer_writes_the_units_for_this_checkout(tmp_path: Path) -> None:
    result = run_env("install-timer", "--no-enable", "--unit-dir", str(tmp_path))

    assert result.returncode == 0
    service = (tmp_path / "glovebox-test-env.service").read_text()
    timer = (tmp_path / "glovebox-test-env.timer").read_text()
    assert f"ExecStart=/usr/bin/env python3 {ROOT}/scripts/env.py ensure-test" in service
    assert "@REPO@" not in service
    assert "OnUnitActiveSec=5min" in timer
    assert "WantedBy=timers.target" in timer


def test_install_timer_is_repeatable(tmp_path: Path) -> None:
    run_env("install-timer", "--no-enable", "--unit-dir", str(tmp_path))
    first = sorted((p.name, p.read_text()) for p in tmp_path.iterdir())

    result = run_env("install-timer", "--no-enable", "--unit-dir", str(tmp_path))

    assert result.returncode == 0
    assert sorted((p.name, p.read_text()) for p in tmp_path.iterdir()) == first


def test_install_timer_will_not_enable_on_a_throwaway_node(tmp_path: Path) -> None:
    result = run_env("install-timer", "--unit-dir", str(tmp_path))

    assert result.returncode == 2
    assert list(tmp_path.iterdir()) == []


def test_an_unknown_tier_is_rejected() -> None:
    result = run_env("reset", tier="production")

    assert result.returncode != 0
    assert "unknown tier" in result.stderr


@pytest.mark.parametrize(
    "command", ["up", "check", "reset", "down", "ensure-test", "install-timer"]
)
def test_every_command_is_listed_in_help(command: str) -> None:
    result = run_env("--help")

    assert command in result.stdout
