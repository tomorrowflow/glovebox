#!/usr/bin/env python3
"""Check a capture of what an agent client handed the model for gate tool calls (ADR 0006).

The capture is one JSON file (`docs/client-adapter-check.md` has its format): the model
input for the tool listing, for a successful result and for an error result. The check
fails when any string or fragment of the hostile corpus (`tests/hostile.py`) or of the
fixture cast (`fixtures/cast.json`) appears in it, or when it holds anything but the
gate's static tool text and fields the gate's output schemas permit. It reads no token and
makes no request; the result holds for the one client and version the capture names.

Usage, from the repository root with the dev install (`.venv/bin/python`):

    .venv/bin/python scripts/client_adapter_check.py CAPTURE.json

Exit status: 0 if all three captures pass, 1 if any leaks or carries more than is
permitted, 2 if the capture file cannot be used. Output is fixed words and counts, never
the text found.
"""

import json
import re
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
CAST = REPO / "fixtures" / "cast.json"

SECTIONS = ("tool_listing", "success_result", "error_result")
CLIENT_FIELDS = ("name", "version", "presentation")
# The cast services that are not sources of attacker-writable text.
NOT_SOURCES = ("inference-local", "inference-external")
MIN_WORD = 4
MAX_NESTING = 5


class Unusable(Exception):
    """The capture file cannot be checked; the message is fixed text."""


class Rules:
    """What may not reach the model, and what may."""

    def __init__(self) -> None:
        # The repository root is on the path only here: the corpus and the gate's schemas
        # are the repository's own, not copies.
        sys.path.insert(0, str(REPO))
        from tests.hostile import FRAGMENT_LENGTH, corpus_fragments, fragments_of

        from glovebox.output import Output
        from glovebox.tool import SPECS

        texts = cast_texts()
        self.fragments = corpus_fragments() | fragments_of(
            text for text in texts if len(text) >= FRAGMENT_LENGTH
        )
        self.words = [
            re.compile(rf"(?<![a-z0-9]){re.escape(text.lower())}(?![a-z0-9])")
            for text in texts
            if MIN_WORD <= len(text) < FRAGMENT_LENGTH
        ]
        self.outputs = [Output, *(spec.output for spec in SPECS.values())]
        self.tool_text = {
            text
            for spec in SPECS.values()
            for text in strings(
                spec.definition.model_dump(by_alias=True, exclude_none=True), keys=True
            )
        }

    def leaks(self, value: Any) -> int:
        """How many corpus fragments and cast words `value` holds, as text or escaped."""
        texts = [json.dumps(value, ensure_ascii=ascii_only).lower() for ascii_only in (True, False)]
        texts += [text.lower() for text in strings(value)]
        fragments = {fragment for fragment in self.fragments if any(fragment in t for t in texts)}
        words = [word for word in self.words if any(word.search(t) for t in texts)]
        return len(fragments) + len(words)

    def permits(self, payload: dict[str, Any]) -> bool:
        """Whether some tool's output schema accepts `payload` exactly, with no coercion."""
        for output in self.outputs:
            try:
                output.model_validate(payload, strict=True)
            except ValueError:
                continue
            return True
        return False

    def unpermitted(self, section: str, value: Any, static: set[str]) -> int:
        """How many parts of `value` are neither static text nor a permitted payload."""
        if section == "tool_listing":
            allowed = self.tool_text | static
            return sum(1 for text in strings(value, keys=True) if text not in allowed)
        return sum(
            1
            for payload, text in payloads_and_text(value)
            if (payload is not None and not self.permits(payload))
            or (payload is None and text not in static)
        )


def cast_texts() -> list[str]:
    """The strings of the fictional cast that stand for attacker-writable upstream text."""
    cast = json.loads(CAST.read_text())
    found = [person[field] for person in cast["people"] for field in ("name", "email", "phone")]
    for name, service in cast["services"].items():
        if name not in NOT_SOURCES:
            found += strings(service)
    return sorted({text for text in found if not is_time(text)})


def is_time(text: str) -> bool:
    """Timestamps are permitted fields (minute precision), so the cast's are not leaks."""
    try:
        datetime.fromisoformat(text)
    except ValueError:
        return False
    return True


def container(text: str, depth: int) -> Any:
    """The JSON object or array a string holds, as a client's serialized copy does, else None."""
    if depth >= MAX_NESTING or not text.lstrip().startswith(("{", "[")):
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def strings(value: Any, keys: bool = False, depth: int = 0) -> list[str]:
    """Every string of `value`, and its object keys if `keys`, reading JSON held in a string."""
    if isinstance(value, str):
        inner = container(value, depth)
        return [value] if inner is None else [*strings(inner, keys, depth + 1), value]
    if isinstance(value, dict):
        found = [text for item in value.values() for text in strings(item, keys, depth)]
        return [*value, *found] if keys else found
    if isinstance(value, list):
        return [text for item in value for text in strings(item, keys, depth)]
    return []


def payloads_and_text(value: Any, depth: int = 0) -> Iterator[tuple[dict[str, Any] | None, str]]:
    """Each gate payload (an object with an `error` field) and each other string or key in a
    result."""
    if isinstance(value, dict) and "error" in value:
        yield value, ""
    elif isinstance(value, dict):
        for key, item in value.items():
            yield None, key
            yield from payloads_and_text(item, depth)
    elif isinstance(value, list):
        for item in value:
            yield from payloads_and_text(item, depth)
    elif isinstance(value, str):
        inner = container(value, depth)
        if inner is None:
            yield None, value
        else:
            yield from payloads_and_text(inner, depth + 1)


def load(path: Path) -> dict[str, Any]:
    try:
        capture = json.loads(path.read_text())
    except OSError:
        raise Unusable("cannot read the file") from None
    except ValueError:
        raise Unusable("not JSON") from None
    if not isinstance(capture, dict):
        raise Unusable("not a JSON object")
    client = capture.get("client")
    if not isinstance(client, dict) or not all(
        isinstance(client.get(field), str) and client[field].strip() for field in CLIENT_FIELDS
    ):
        raise Unusable("client needs a non-empty name, version and presentation")
    static = capture.get("static_text", [])
    if not isinstance(static, list) or not all(isinstance(text, str) for text in static):
        raise Unusable("static_text must be a list of strings")
    for section in SECTIONS:
        if section not in capture:
            raise Unusable(f"{section} is missing")
    if not strings(capture["tool_listing"], keys=True):
        raise Unusable("tool_listing holds no text")
    return capture


def check_kinds(capture: dict[str, Any]) -> None:
    """A success capture holds only `none` payloads, an error capture none of them."""
    for section, is_success in (("success_result", True), ("error_result", False)):
        for payload, _ in payloads_and_text(capture[section]):
            if payload is not None and (payload["error"] == "none") != is_success:
                kind = "a success" if is_success else "an error"
                raise Unusable(f"{section}: not {kind} result")


def report(rules: Rules, capture: dict[str, Any]) -> tuple[list[str], bool]:
    """One line per finding, and whether every section passed."""
    static = set(capture.get("static_text", []))
    lines: list[str] = []
    passed = True
    for section in SECTIONS:
        value = capture[section]
        leaked = rules.leaks(value)
        unpermitted = rules.unpermitted(section, value, static)
        if leaked:
            lines.append(f"{section}: leak ({leaked} matches)")
        if unpermitted:
            lines.append(f"{section}: not permitted ({unpermitted} findings)")
        if leaked or unpermitted:
            passed = False
        else:
            reached = section == "tool_listing" or any(
                payload is not None for payload, _ in payloads_and_text(value)
            )
            lines.append(
                f"{section}: ok" + ("" if reached else ", no gate payload reached the model")
            )
    lines.append("check: ok" if passed else "check: failed")
    return lines, passed


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: client_adapter_check.py CAPTURE.json", file=sys.stderr)
        return 2
    try:
        capture = load(Path(sys.argv[1]))
        check_kinds(capture)
    except Unusable as problem:
        print(f"capture: {problem}", file=sys.stderr)
        return 2
    lines, passed = report(Rules(), capture)
    print("\n".join(lines))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
