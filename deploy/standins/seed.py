"""Seed the fictional fixture cast into a state directory (ADR 0007).

The output depends only on the fixture file, so seeding twice gives identical
state. Files that already hold the right bytes are left untouched and files the
cast does not name are removed.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path

MANIFEST = "manifest.json"


def canonical(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def build_files(cast: dict[str, object]) -> dict[str, bytes]:
    services = cast["services"]
    assert isinstance(services, dict)
    files = {"people.json": canonical(cast["people"])}
    for name, content in services.items():
        files[f"{name}.json"] = canonical(content)
    return files


def write_if_changed(path: Path, content: bytes) -> None:
    if path.exists() and path.read_bytes() == content:
        return
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(content)
    os.replace(temporary, path)


def seed(fixtures: Path, state: Path) -> None:
    files = build_files(json.loads(fixtures.read_text()))
    digests = {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}
    overall = hashlib.sha256(canonical(digests)).hexdigest()
    files[MANIFEST] = canonical({"files": digests, "state_sha256": overall})
    state.mkdir(parents=True, exist_ok=True)
    for stale in state.iterdir():
        if stale.name not in files:
            stale.unlink()
    for name, content in files.items():
        if name != MANIFEST:
            write_if_changed(state / name, content)
    # The manifest goes last: its presence means the state is complete.
    write_if_changed(state / MANIFEST, files[MANIFEST])


def dump(state: Path) -> str:
    """One canonical JSON document of everything seeded, for comparing two states."""
    document = {
        "manifest": json.loads((state / MANIFEST).read_text()),
        "files": {
            path.name: json.loads(path.read_text())
            for path in sorted(state.glob("*.json"))
            if path.name != MANIFEST
        },
    }
    return canonical(document).decode()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixtures", type=Path, default=Path("/fixtures/cast.json"))
    parser.add_argument("--state", type=Path, default=Path("/state/seed"))
    parser.add_argument("--dump", action="store_true", help="print the seeded state and exit")
    args = parser.parse_args()
    if not args.dump:
        seed(args.fixtures, args.state)
    print(dump(args.state) if args.dump else "seeded")


if __name__ == "__main__":
    main()
