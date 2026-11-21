"""The operator's classifier configuration: topic list, deadline range and inference settings.

It is one JSON file named by `GLOVEBOX_CLASSIFIER_CONFIG`, read once at startup and never
reachable through MCP. Topic names and descriptions live here and brief the classifier;
only topic IDs are ever served. Error messages never contain a value from the file.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from glovebox.classification import DeadlineRange, Topic
from glovebox.config import ConfigError
from glovebox.formats import MAX_TOPICS, MINUTE_FORMAT, RESERVED_TOPICS, TOPIC_ID_PATTERN

BACKENDS = ("stub",)
PROFILE_PATTERN = re.compile(r"[a-z0-9._-]{1,32}")
TOPIC_ID = re.compile(TOPIC_ID_PATTERN)
MAX_NAME_CHARS = 100
MAX_DESCRIPTION_CHARS = 500
MAX_FILE_BYTES = 256 * 1024

# Bumped when what the gate does with a proposal changes, so old classifications go stale.
VALIDATION_VERSION = 1


@dataclass(frozen=True)
class ClassifierConfig:
    topics: tuple[Topic, ...] = ()
    deadline_range: DeadlineRange = DeadlineRange()
    # None means no classifier is configured: every item's classification is unavailable.
    backend: str | None = None
    profile: str = ""

    def digest(self) -> str:
        """Identifies everything a stored classification depends on besides the item's text:
        the topic list, the deadline range and the inference settings."""
        range_ = self.deadline_range
        document = {
            "version": VALIDATION_VERSION,
            "topics": [[topic.id, topic.name, topic.description] for topic in self.topics],
            "deadline_range": [_spell(range_.start), _spell(range_.end)],
            "inference": [self.backend, self.profile],
        }
        text = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()


def _spell(moment: datetime | None) -> str | None:
    return None if moment is None else moment.strftime(MINUTE_FORMAT)


def load_classifier_config(path: Path | None) -> ClassifierConfig:
    if path is None:
        return ClassifierConfig()
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ConfigError("the classifier configuration file is too large")
        document = json.loads(data)
    except (OSError, ValueError, RecursionError):
        raise ConfigError("the classifier configuration file cannot be read as JSON") from None
    return parse_classifier_config(document)


def parse_classifier_config(document: object) -> ClassifierConfig:
    section = _object(document, {"topics", "deadline_range", "inference"}, "the configuration")
    topics = _topics(section.get("topics", []))
    range_ = _deadline_range(section.get("deadline_range", {}))
    inference = _object(section.get("inference", {}), {"backend", "profile"}, "inference")
    backend = inference.get("backend")
    if backend is not None and backend not in BACKENDS:
        raise ConfigError("inference.backend is not a known backend")
    profile = inference.get("profile", "")
    if not isinstance(profile, str) or (profile and not PROFILE_PATTERN.fullmatch(profile)):
        raise ConfigError("inference.profile must be 1 to 32 of a-z, 0-9, dot, dash, underscore")
    return ClassifierConfig(topics, range_, backend, profile)


def _object(value: object, keys: set[str], what: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not set(value) <= keys:
        raise ConfigError(f"{what} must be an object with only the documented keys")
    return value


def _topics(value: object) -> tuple[Topic, ...]:
    if not isinstance(value, list) or len(value) > MAX_TOPICS:
        raise ConfigError(f"topics must be a list of at most {MAX_TOPICS} topics")
    topics: list[Topic] = []
    for entry in value:
        fields = _object(entry, {"id", "name", "description"}, "a topic")
        topic_id, name, description = (fields.get(key) for key in ("id", "name", "description"))
        if not isinstance(topic_id, str) or TOPIC_ID.fullmatch(topic_id) is None:
            raise ConfigError("a topic ID must match " + TOPIC_ID_PATTERN)
        if topic_id in RESERVED_TOPICS:
            raise ConfigError("a topic ID cannot be one of: " + ", ".join(RESERVED_TOPICS))
        if any(topic.id == topic_id for topic in topics):
            raise ConfigError("topic IDs must be unique")
        if not isinstance(name, str) or not 0 < len(name) <= MAX_NAME_CHARS:
            raise ConfigError(f"a topic name must be 1 to {MAX_NAME_CHARS} characters")
        description = "" if description is None else description
        if not isinstance(description, str) or len(description) > MAX_DESCRIPTION_CHARS:
            raise ConfigError(f"a topic description must be at most {MAX_DESCRIPTION_CHARS}")
        topics.append(Topic(topic_id, name, description))
    return tuple(topics)


def _deadline_range(value: object) -> DeadlineRange:
    fields = _object(value, {"from", "to"}, "deadline_range")
    start, end = (_minute(fields.get(key)) for key in ("from", "to"))
    if start is not None and end is not None and start > end:
        raise ConfigError("deadline_range.from must not be after deadline_range.to")
    return DeadlineRange(start, end)


def _minute(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str):
            raise ValueError
        return datetime.strptime(value, MINUTE_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        raise ConfigError(
            "deadline_range values must be minute timestamps like 2026-01-31T09:00Z"
        ) from None
