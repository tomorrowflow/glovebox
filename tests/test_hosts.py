"""The gate's allowed hosts (GLB-1.13): loopback by default, more only when configured.

Each test runs the real service and sends real HTTP requests with a chosen `Host` header,
the way a reverse proxy in front of the gate would present one.
"""

import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

import httpx2
import pytest

from glovebox.config import ConfigError, Settings
from tests.conftest import GateStarter, clean_environment, free_port, run_gate

TAILNET_HOST = "florians-mac-mini.tail974337.ts.net:8765"
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


def initialize(url: str, *, host: str | None = None, token: str | None = None) -> httpx2.Response:
    headers = {"Accept": "application/json, text/event-stream"}
    if host is not None:
        headers["Host"] = host
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return httpx2.post(url, json=INITIALIZE, headers=headers, timeout=30)


def test_the_default_serves_loopback_hosts_and_refuses_every_other(
    start_gate: GateStarter,
) -> None:
    with start_gate() as gate:
        port = gate.url.split(":")[2].split("/")[0]
        assert initialize(gate.url, token=gate.token).status_code == 200
        for host in (f"localhost:{port}", f"[::1]:{port}"):
            assert initialize(gate.url, host=host, token=gate.token).status_code == 200
        for host in (TAILNET_HOST, "florians-mac-mini.tail974337.ts.net", "evil.example:8765"):
            refused = initialize(gate.url, host=host, token=gate.token)
            assert refused.status_code == 421


def test_a_configured_host_is_served_to_an_issued_token(tmp_path: Path) -> None:
    accounts = {"fixture": tmp_path / "none.json"}
    with run_gate(
        tmp_path / "glovebox.db",
        accounts=accounts,
        extra_env={"GLOVEBOX_ALLOWED_HOSTS": TAILNET_HOST},
    ) as gate:
        reply = initialize(gate.url, host=TAILNET_HOST, token=gate.token)

        assert reply.status_code == 200
        assert '"glovebox"' in reply.text
        # Loopback still works, and a host that is not listed is still refused.
        assert initialize(gate.url, token=gate.token).status_code == 200
        for host in ("other.tail974337.ts.net:8765", "florians-mac-mini.tail974337.ts.net:9"):
            assert initialize(gate.url, host=host, token=gate.token).status_code == 421


def test_a_configured_host_still_needs_a_valid_token(tmp_path: Path) -> None:
    with run_gate(
        tmp_path / "glovebox.db",
        accounts={"fixture": tmp_path / "none.json"},
        extra_env={"GLOVEBOX_ALLOWED_HOSTS": TAILNET_HOST},
    ) as gate:
        for token in (None, "wrong-token-0123456789abcdef", gate.token + "x"):
            reply = initialize(gate.url, host=TAILNET_HOST, token=token)
            assert reply.status_code == 401
        # The token check comes first, so a caller without one learns nothing about hosts.
        assert initialize(gate.url, host="evil.example:8765").status_code == 401


def test_several_hosts_can_be_listed(tmp_path: Path) -> None:
    other = "second.tail974337.ts.net:8765"
    with run_gate(
        tmp_path / "glovebox.db",
        accounts={"fixture": tmp_path / "none.json"},
        extra_env={"GLOVEBOX_ALLOWED_HOSTS": f"{TAILNET_HOST}, {other}"},
    ) as gate:
        for host in (TAILNET_HOST, other):
            assert initialize(gate.url, host=host, token=gate.token).status_code == 200


@pytest.mark.parametrize(
    "value",
    [
        "*",
        "*:8765",
        "*.ts.net:8765",
        "host.ts.net:*",
        "host.ts.net:8765,*",
        "host.ts.net:8765,,other.ts.net",
        "https://host.ts.net:8765",
        "host.ts.net:8765/mcp",
        "user@host.ts.net",
        "host ts.net",
        "host.ts.net:99999",
        "host.ts.net:",
        "[::1",
    ],
)
def test_a_wildcard_or_malformed_host_is_refused_without_echoing_it(
    tmp_path: Path, value: str
) -> None:
    with pytest.raises(ConfigError) as refused:
        Settings.from_env({"GLOVEBOX_DB": str(tmp_path / "g.db"), "GLOVEBOX_ALLOWED_HOSTS": value})

    assert value not in str(refused.value)


@pytest.mark.parametrize("value", ["", "  "])
def test_a_blank_setting_means_loopback_only(tmp_path: Path, value: str) -> None:
    settings = Settings.from_env(
        {"GLOVEBOX_DB": str(tmp_path / "g.db"), "GLOVEBOX_ALLOWED_HOSTS": value}
    )

    assert settings.allowed_hosts == ()


def test_the_service_refuses_to_start_with_a_wildcard_host(tmp_path: Path) -> None:
    env: Mapping[str, str] = clean_environment(
        tmp_path / "g.db", GLOVEBOX_PORT=str(free_port()), GLOVEBOX_ALLOWED_HOSTS="*"
    )

    result = subprocess.run(
        [sys.executable, "-m", "glovebox"],
        env=dict(env),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 1
    assert "GLOVEBOX_ALLOWED_HOSTS" in result.stderr
