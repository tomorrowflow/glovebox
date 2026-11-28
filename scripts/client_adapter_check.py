#!/usr/bin/env python3
"""Check a capture of what an agent client handed the model for gate tool calls (ADR 0006).

The capture is one JSON file (`docs/client-adapter-check.md` has its format): the model
input for the tool listing, for a successful result and for an error result, and the text
the client itself adds around the gate's (`static_text` and `wrappers`). The check fails
when a string of the hostile corpus (`tests/hostile.py`) or of the fixture cast
(`fixtures/cast.json`), or a fragment of one, appears in what came from the gate, or when
the capture holds anything but the gate's tool text, fields its output schemas permit and
the client text the capture declares. It reads no token and makes no request; the result
holds for the one client and version the capture names.

Usage, from the repository root with the dev install (`.venv/bin/python`):

    .venv/bin/python scripts/client_adapter_check.py [--explain] CAPTURE.json

Exit status: 0 if all three captures pass, 1 if any leaks or carries more than is
permitted, 2 if the capture file cannot be used. Output is fixed words and counts, never
the text found. `--explain` also prints, JSON-quoted, the text that is neither the gate's
nor declared, which can be hostile text: read it, do not paste it into an agent.
"""

import argparse
import json
import re
import sys
from collections.abc import Iterable, Iterator
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

REPO = Path(__file__).resolve().parent.parent
CAST = REPO / "fixtures" / "cast.json"

SECTIONS = ("tool_listing", "success_result", "error_result")
CLIENT_FIELDS = ("name", "version", "presentation")
# The cast services that are not sources of attacker-writable text.
NOT_SOURCES = ("inference-local", "inference-external")
MIN_WORD = 4
MAX_NESTING = 5
EXPLAIN_LENGTH = 160

# Placeholders of declared client text: a hex string, which a client uses for an ID, and, in
# a wrapper, the same hex string wherever `{id}` appears.
HEX_MARK = "{hex}"
HEX = "[0-9a-f]{8,64}"
WORD = re.compile(r"\{hex\}|[^\W_]+")
CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
PLACEHOLDER = re.compile(r"(\{id\}|\{hex\}|\s+)")


class Unusable(Exception):
    """The capture file cannot be checked; the message is fixed text."""


class Part(NamedTuple):
    """One piece of model input: a gate payload (an object with an `error` field) or text."""

    payload: dict[str, Any] | None
    text: str


class Examined(NamedTuple):
    leaked: int
    unpermitted: int
    reached: bool
    undeclared: list[str]


def words(text: str, placeholders: bool = False) -> list[tuple[str, int, int]]:
    """The words of `text` with their spans: lower case, split at `_`, punctuation and
    camelCase, so that a client's renaming of an identifier keeps its words."""
    found: list[tuple[str, int, int]] = []
    for match in WORD.finditer(text):
        word, start = match.group(), match.start()
        if word == HEX_MARK:
            if placeholders:
                found.append((word, start, match.end()))
            continue
        cuts = [0, *(cut.start() for cut in CAMEL.finditer(word)), len(word)]
        for first, last in zip(cuts, cuts[1:], strict=False):
            found.append((word[first:last].lower(), start + first, start + last))
    return found


class Vocabulary:
    """Sequences of words that are allowed. A text is covered where it reads, word for word,
    as one of them; numbers are always covered, since they carry no text."""

    def __init__(self, sequences: Iterable[list[str]]) -> None:
        self._by_first: dict[str, list[list[str]]] = {}
        for sequence in sequences:
            if sequence:
                self._by_first.setdefault(sequence[0], []).append(sequence)

    def _covering(self, found: list[tuple[str, int, int]], at: int) -> int:
        """The length of the longest allowed sequence that `found` starts with at `at`."""
        word = found[at][0]
        candidates = self._by_first.get(word, [])
        if re.fullmatch(HEX, word):
            candidates = [*candidates, *self._by_first.get(HEX_MARK, [])]
        for sequence in sorted(candidates, key=len, reverse=True):
            tail = found[at : at + len(sequence)]
            if len(tail) == len(sequence) and all(
                wanted == seen or (wanted == HEX_MARK and re.fullmatch(HEX, seen))
                for wanted, (seen, _, _) in zip(sequence, tail, strict=True)
            ):
                return len(sequence)
        return 0

    def mask(self, text: str) -> tuple[str, bool]:
        """`text` with its covered words blanked, and whether any word was left."""
        found = words(text)
        characters = list(text)
        left = False
        at = 0
        while at < len(found):
            length = self._covering(found, at) or (1 if found[at][0].isdigit() else 0)
            if not length:
                left = True
                at += 1
                continue
            for _, start, end in found[at : at + length]:
                characters[start:end] = " " * (end - start)
            at += length
        return "".join(characters), left


class Declared:
    """The text the capture says the client itself adds, and how it wraps results."""

    def __init__(self, static_text: list[str], wrappers: list[dict[str, str]]) -> None:
        self.static_text = static_text
        self.wrappers = wrappers
        self._patterns = [wrapper_pattern(item["prefix"], item["suffix"]) for item in wrappers]
        self.vocabulary_sequences = [
            [word for word, _, _ in words(text, placeholders=True)] for text in static_text
        ]

    def texts(self) -> list[str]:
        """All declared text, placeholders blanked."""
        found = [*self.static_text]
        for item in self.wrappers:
            found += [item["prefix"], item["suffix"]]
        return [text.replace(HEX_MARK, " ").replace("{id}", " ") for text in found]

    def unwrap(self, text: str) -> str | None:
        """What sits between a declared prefix and suffix, if `text` is wrapped."""
        for pattern in self._patterns:
            match = pattern.fullmatch(text.strip())
            if match:
                return match.group("body")
        return None

    def parts(self, value: Any, payloads: bool, depth: int = 0) -> Iterator[Part]:
        """The payloads and texts of `value`: object keys and strings, reading what is
        inside a declared wrapper and JSON held in a string."""
        if isinstance(value, dict):
            if payloads and "error" in value:
                yield Part(value, "")
                return
            for key, item in value.items():
                yield Part(None, key)
                yield from self.parts(item, payloads, depth)
        elif isinstance(value, list):
            for item in value:
                yield from self.parts(item, payloads, depth)
        elif isinstance(value, str):
            body = self.unwrap(value) if depth < MAX_NESTING else None
            inner = container(value, depth) if body is None else None
            if body is not None:
                yield from self.parts(body, payloads, depth + 1)
            elif inner is not None:
                yield from self.parts(inner, payloads, depth + 1)
            else:
                yield Part(None, value)


def wrapper_pattern(prefix: str, suffix: str) -> re.Pattern[str]:
    """The pattern of a text that is `prefix`, a body, then `suffix`. Whitespace is flexible,
    `{hex}` is any hex string and `{id}` a hex string that is the same each time."""
    seen: list[str] = []

    def pattern(template: str) -> str:
        pieces = []
        for part in PLACEHOLDER.split(template):
            if part == "{id}":
                pieces.append("(?P=id)" if seen else f"(?P<id>{HEX})")
                seen.append(part)
            elif part == HEX_MARK:
                pieces.append(HEX)
            elif part.isspace():
                pieces.append(r"\s*")
            else:
                pieces.append(re.escape(part))
        return "".join(pieces)

    return re.compile(f"{pattern(prefix)}(?P<body>.*?){pattern(suffix)}", re.DOTALL)


class Rules:
    """What may not reach the model, and what may."""

    def __init__(self) -> None:
        # The repository root is on the path only here: the corpus and the gate's schemas
        # are the repository's own, not copies.
        sys.path.insert(0, str(REPO))
        from tests.hostile import CORPUS, FRAGMENT_LENGTH, corpus_fragments, fragments_of

        from glovebox.output import Output
        from glovebox.tool import SPECS

        texts = cast_texts()
        self.fragments = corpus_fragments() | fragments_of(
            text for text in texts if len(text) >= FRAGMENT_LENGTH
        )
        self.wholes = [
            [form.lower() for form in forms(text)]
            for text in [*CORPUS.values(), *(t for t in texts if len(t) >= FRAGMENT_LENGTH)]
        ]
        self.words = [
            re.compile(rf"(?<![a-z0-9]){re.escape(text.lower())}(?![a-z0-9])")
            for text in texts
            if MIN_WORD <= len(text) < FRAGMENT_LENGTH
        ]
        self.outputs = [Output, *(spec.output for spec in SPECS.values())]
        self.tool_sequences = [
            [word for word, _, _ in words(text)]
            for spec in SPECS.values()
            for text in strings(
                spec.definition.model_dump(by_alias=True, exclude_none=True), keys=True
            )
        ]

    def whole_matches(self, texts: list[str]) -> int:
        """How many whole corpus or cast strings occur in `texts` (lower case)."""
        wholes = sum(1 for each in self.wholes if any(f in t for f in each for t in texts))
        return wholes + sum(1 for word in self.words if any(word.search(t) for t in texts))

    def fragment_matches(self, texts: list[str]) -> int:
        """How many corpus fragments occur in `texts` (lower case)."""
        return sum(1 for fragment in self.fragments if any(fragment in t for t in texts))

    def permits(self, payload: dict[str, Any]) -> bool:
        """Whether some tool's output schema accepts `payload` exactly, with no coercion."""
        for output in self.outputs:
            try:
                output.model_validate(payload, strict=True)
            except ValueError:
                continue
            return True
        return False

    def examine(self, section: str, value: Any, declared: Declared) -> Examined:
        """What of `value` is neither declared client text nor the gate's, and what leaks.

        Whole corpus and cast strings are looked for in everything of the section. Fragments
        are looked for only in what is not declared: the client's own wording has fragments
        of the corpus in it by chance.
        """
        listing = section == "tool_listing"
        vocabulary = Vocabulary(
            [*declared.vocabulary_sequences, *(self.tool_sequences if listing else [])]
        )
        derived: list[str] = []
        undeclared: list[str] = []
        unpermitted = 0
        payloads = False
        for part in declared.parts(value, payloads=not listing):
            if part.payload is not None:
                payloads = True
                if not self.permits(part.payload):
                    unpermitted += 1
                derived += [json.dumps(part.payload, ensure_ascii=a) for a in (True, False)]
                derived += strings(part.payload)
                continue
            masked, left = vocabulary.mask(part.text)
            if left:
                unpermitted += 1
                derived.append(masked)
                undeclared.append(" ".join(masked.split()))
        everything = [json.dumps(value, ensure_ascii=a) for a in (True, False)] + strings(value)
        leaked = self.whole_matches([t.lower() for t in everything])
        leaked += self.fragment_matches([t.lower() for t in derived])
        return Examined(leaked, unpermitted, listing or payloads, undeclared)

    def declared_text(self, declared: Declared) -> tuple[int, int]:
        """The whole corpus or cast strings, and the corpus fragments, in declared text."""
        texts = [text.lower() for text in declared.texts()]
        return self.whole_matches(texts), self.fragment_matches(texts)


def forms(text: str) -> list[str]:
    """The ways a string can appear: as is, and JSON-escaped."""
    return [text, *(json.dumps(text, ensure_ascii=a)[1:-1] for a in (True, False))]


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


def load(path: Path) -> tuple[dict[str, Any], Declared]:
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
    wrappers = capture.get("wrappers", [])
    if not isinstance(wrappers, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("prefix"), str)
        and item["prefix"].strip()
        and isinstance(item.get("suffix"), str)
        for item in wrappers
    ):
        raise Unusable("wrappers must be a list of objects with a prefix and a suffix")
    for section in SECTIONS:
        if section not in capture:
            raise Unusable(f"{section} is missing")
    declared = Declared(static, wrappers)
    if not any(declared.parts(capture["tool_listing"], payloads=False)):
        raise Unusable("tool_listing holds no text")
    return capture, declared


def check_kinds(capture: dict[str, Any], declared: Declared) -> None:
    """A success capture holds only `none` payloads, an error capture none of them."""
    for section, is_success in (("success_result", True), ("error_result", False)):
        for part in declared.parts(capture[section], payloads=True):
            if part.payload is not None and (part.payload["error"] == "none") != is_success:
                kind = "a success" if is_success else "an error"
                raise Unusable(f"{section}: not {kind} result")


def explained(text: str) -> str:
    return json.dumps(text[:EXPLAIN_LENGTH])


def report(
    rules: Rules, capture: dict[str, Any], declared: Declared, explain: bool
) -> tuple[list[str], bool]:
    """One line per finding, and whether every section passed."""
    lines: list[str] = []
    passed = True
    for section in SECTIONS:
        examined = rules.examine(section, capture[section], declared)
        if examined.leaked:
            lines.append(f"{section}: leak ({examined.leaked} matches)")
        if examined.unpermitted:
            lines.append(f"{section}: not permitted ({examined.unpermitted} findings)")
        if examined.leaked or examined.unpermitted:
            passed = False
        else:
            note = "" if examined.reached else ", no gate payload reached the model"
            lines.append(f"{section}: ok{note}")
        if explain:
            lines += [
                f"  {section}: not declared {explained(text)}" for text in examined.undeclared
            ]
    whole, fragments = rules.declared_text(declared)
    if whole:
        lines.append(f"static_text: leak ({whole} matches)")
        passed = False
    if fragments:
        lines.append(f"static_text: corpus match ({fragments})")
    lines.append("check: ok" if passed else "check: failed")
    return lines, passed


def main() -> int:
    parser = argparse.ArgumentParser(description="Check a capture of the model input of a client.")
    parser.add_argument(
        "capture", type=Path, help="the capture file (docs/client-adapter-check.md)"
    )
    parser.add_argument("--explain", action="store_true", help="also print undeclared text")
    arguments = parser.parse_args()
    try:
        capture, declared = load(arguments.capture)
        check_kinds(capture, declared)
    except Unusable as problem:
        print(f"capture: {problem}", file=sys.stderr)
        return 2
    lines, passed = report(Rules(), capture, declared, arguments.explain)
    print("\n".join(lines))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
