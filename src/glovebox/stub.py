"""The deterministic stub classifier, for tests and the fixture stack. It is not a model.

Without a script it answers by simple keyword rules. A script (JSON, `{"rules": [...]}`)
overrides that: the first rule whose `match` text occurs in an item's text decides, and
it either returns its `output` verbatim (a string, or an object sent as JSON) or fails
the way the rule says (`unavailable`, or `crash` for an unexpected error). That lets a
test make the stub return prose, extra fields or hostile strings. The probe, when set,
records each call as a digest of the text and the topic IDs it received, never the text.
"""

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from glovebox.classification import ClassifierUnavailable, Topic
from glovebox.config import ConfigError

DUE = re.compile(r"due ([0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}Z)", re.IGNORECASE)


@dataclass(frozen=True)
class Rule:
    match: str
    output: str | None
    unavailable: bool
    crash: bool


class StubClassifier:
    def __init__(self, rules: Sequence[Rule] = (), probe: Path | None = None) -> None:
        self._rules = tuple(rules)
        self._probe = probe

    @classmethod
    def from_files(cls, script: Path | None, probe: Path | None) -> "StubClassifier":
        return cls(_load_rules(script) if script else (), probe)

    def classify(self, text: str, topics: Sequence[Topic]) -> str:
        self._record(text, topics)
        for rule in self._rules:
            if rule.match in text:
                if rule.unavailable:
                    raise ClassifierUnavailable
                if rule.crash:
                    raise RuntimeError(text)  # a failure whose message holds the item's text
                return rule.output or ""
        return _by_keywords(text, topics)

    def _record(self, text: str, topics: Sequence[Topic]) -> None:
        if self._probe is None:
            return
        entry = {
            "text_sha256": hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest(),
            "topics": [topic.id for topic in topics],
        }
        with self._probe.open("a") as probe:
            probe.write(json.dumps(entry) + "\n")


def _by_keywords(text: str, topics: Sequence[Topic]) -> str:
    lowered = text.lower()
    due = DUE.search(text)
    return json.dumps(
        {
            "topic": next((topic.id for topic in topics if topic.id in lowered), "other"),
            "reply_needed": "yes" if "?" in text else "no",
            "action_requested": "yes" if "please" in lowered else "no",
            "deadline": due.group(1) if due else "unknown",
        }
    )


def _load_rules(path: Path) -> list[Rule]:
    try:
        document: Any = json.loads(path.read_bytes())
        rules = [_rule(entry) for entry in document["rules"]]
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        raise ConfigError("the stub classifier script is unusable") from None
    return rules


def _rule(entry: dict[str, Any]) -> Rule:
    output = entry.get("output")
    if output is not None and not isinstance(output, str):
        output = json.dumps(output)
    return Rule(
        match=str(entry["match"]),
        output=output,
        unavailable=bool(entry.get("unavailable")),
        crash=bool(entry.get("crash")),
    )
