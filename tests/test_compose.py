"""Where the published gate port listens on each tier (ADR 0007).

The gate accepts only issued agent tokens, but its allowed-hosts setting is loopback only,
so no tier publishes it beyond loopback.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None, reason="needs the docker CLI to render compose files"
)


def gate_ports(*files: str) -> list[dict[str, object]]:
    env = {**os.environ, "GLOVEBOX_TEST_TOKEN": "unused"}
    env.pop("GLOVEBOX_ENV_PUBLISH_PORT", None)
    command = ["docker", "compose", "--project-name", "glovebox-render"]
    for file in files:
        command += ["-f", file]
    rendered = subprocess.run(
        [*command, "config", "--format", "json"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    ports: list[dict[str, object]] = json.loads(rendered.stdout)["services"]["gate"]["ports"]
    return ports


def test_the_test_gate_is_published_on_8765_on_loopback_only() -> None:
    ports = gate_ports("compose.yaml", "compose.test.yaml")

    assert len(ports) == 1
    assert ports[0]["host_ip"] == "127.0.0.1"
    assert str(ports[0]["published"]) == "8765"
    assert ports[0]["target"] == 8765


def test_the_throwaway_gate_takes_a_free_loopback_port() -> None:
    ports = gate_ports("compose.yaml")

    assert ports[0]["host_ip"] == "127.0.0.1"
    assert not ports[0].get("published")
