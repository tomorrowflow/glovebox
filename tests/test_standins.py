"""The fixture seed and the stand-ins for outside services (ADR 0007).

Seam: the seed command writes the seeded state directory, and the stand-ins
answer over HTTP from it.
"""

import importlib.util
import json
import re
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import HTTPServer
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "deploy" / "standins" / "seed.py"
STANDINS = ROOT / "deploy" / "standins" / "standins.py"
CAST = ROOT / "fixtures" / "cast.json"

OUTSIDE_SERVICES = [
    "mailbox-mail",
    "mailbox-calendar",
    "o365-mail",
    "o365-calendar",
    "signal",
    "imessage",
    "whatsapp",
    "inference-local",
    "inference-external",
]


def run_seed(state: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SEED), "--fixtures", str(CAST), "--state", str(state), *extra],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def load_standins() -> ModuleType:
    spec = importlib.util.spec_from_file_location("glovebox_standins", STANDINS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def serve(service: str, state: Path) -> Iterator[str]:
    server: HTTPServer = load_standins().make_server(service, state, "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def fetch(url: str, data: bytes | None = None) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=5) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def test_seeding_twice_gives_identical_state(tmp_path: Path) -> None:
    first, second = tmp_path / "first", tmp_path / "second"
    assert run_seed(first).returncode == 0
    assert run_seed(second).returncode == 0
    assert run_seed(first).returncode == 0

    dump_first = run_seed(first, "--dump").stdout
    dump_second = run_seed(second, "--dump").stdout

    assert dump_first == dump_second
    assert json.loads(dump_first)["manifest"]["state_sha256"]


def test_reseeding_restores_state_that_drifted(tmp_path: Path) -> None:
    run_seed(tmp_path)
    expected = run_seed(tmp_path, "--dump").stdout
    (tmp_path / "signal.json").write_text('{"drifted": true}')
    (tmp_path / "stale.json").write_text("{}")

    run_seed(tmp_path)

    assert run_seed(tmp_path, "--dump").stdout == expected


def load_seed() -> ModuleType:
    spec = importlib.util.spec_from_file_location("glovebox_seed", SEED)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_overlapping_seeds_do_not_fail_or_corrupt_the_state(tmp_path: Path) -> None:
    """A manual reset can overlap the timer's reseed; both must finish and agree."""
    seed = load_seed()
    seed.seed(CAST, tmp_path)
    expected = seed.dump(tmp_path)
    failures: list[BaseException] = []

    def reseed_repeatedly(worker: int) -> None:
        try:
            for round_number in range(30):
                # Different content to restore each time, so every seed has files to write.
                (tmp_path / "signal.json").write_text(
                    f'{{"worker": {worker}, "round": {round_number}}}'
                )
                seed.seed(CAST, tmp_path)
        except BaseException as error:
            failures.append(error)

    workers = [threading.Thread(target=reseed_repeatedly, args=(n,)) for n in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert failures == []
    seed.seed(CAST, tmp_path)
    assert seed.dump(tmp_path) == expected


def test_seeding_leaves_no_temporary_files_behind(tmp_path: Path) -> None:
    run_seed(tmp_path)
    (tmp_path / "signal.json.tmp").write_text("left over from a seed that crashed")
    (tmp_path / "signal.json").write_text('{"drifted": true}')

    run_seed(tmp_path)

    assert [path.name for path in tmp_path.glob("*.tmp")] == []
    assert not (tmp_path / "signal.json.tmp").exists()


def test_every_outside_service_has_seeded_fixtures(tmp_path: Path) -> None:
    run_seed(tmp_path)

    files = {path.stem for path in tmp_path.glob("*.json")}

    assert set(OUTSIDE_SERVICES) <= files


def test_the_cast_is_fictional() -> None:
    text = CAST.read_text()

    addresses = re.findall(r"[\w.+-]+@[\w.-]+", text)
    assert addresses
    assert all(address.endswith("@example.test") for address in addresses)
    phones = re.findall(r"\+\d[\d ]{6,}", text)
    assert phones
    assert all(re.fullmatch(r"\+1555010\d", phone) for phone in phones)
    assert not re.search(r"https?://(?!example\.test)", text)


def test_a_stand_in_serves_its_seeded_fixture(tmp_path: Path) -> None:
    run_seed(tmp_path)
    with serve("signal", tmp_path) as url:
        status, body = fetch(url + "/")

        assert status == 200
        assert json.loads(body) == json.loads((tmp_path / "signal.json").read_text())


def test_a_stand_in_is_unhealthy_until_seeded(tmp_path: Path) -> None:
    with serve("mailbox-mail", tmp_path) as url:
        assert fetch(url + "/healthz")[0] == 503

        run_seed(tmp_path)

        assert fetch(url + "/healthz")[0] == 200


@pytest.mark.parametrize("service", ["inference-local", "inference-external"])
def test_inference_stand_ins_give_the_same_canned_answer_every_time(
    service: str, tmp_path: Path
) -> None:
    run_seed(tmp_path)
    with serve(service, tmp_path) as url:
        endpoint = url + "/v1/chat/completions"
        first = fetch(endpoint, b'{"messages": [{"role": "user", "content": "one"}]}')
        second = fetch(endpoint, b'{"messages": [{"role": "user", "content": "two"}]}')

        assert first[0] == 200
        assert first == second
        assert json.loads(first[1])["choices"][0]["message"]["content"] == "unknown"
