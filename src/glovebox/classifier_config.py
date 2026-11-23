"""The operator's classifier configuration: topic list, deadline range and inference settings.

It is one JSON file named by `GLOVEBOX_CLASSIFIER_CONFIG`, read once at startup and never
reachable through MCP. Topic names and descriptions live here and brief the classifier;
only topic IDs are ever served. Error messages never contain a value from the file.

Inference is local unless the file lists an account under `inference.external.accounts`:
that is the one way an item's text leaves the host, it is per account, and there is no
fallback from one backend to the other (ADR 0002).
"""

import hashlib
import ipaddress
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from glovebox.access import ACCOUNT_NAME
from glovebox.classification import DeadlineRange, Topic
from glovebox.config import ConfigError
from glovebox.formats import MAX_TOPICS, MINUTE_FORMAT, RESERVED_TOPICS, TOPIC_ID_PATTERN, minute

BACKENDS = ("local", "stub")
PROFILE_PATTERN = re.compile(r"[a-z0-9._-]{1,32}")
MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,99}")
TOPIC_ID = re.compile(TOPIC_ID_PATTERN)
MAX_NAME_CHARS = 100
MAX_DESCRIPTION_CHARS = 500
MAX_URL_CHARS = 200
MAX_FILE_BYTES = 256 * 1024

# What the local runtime is asked for when the file says nothing: Ollama's address on the
# gateway host and a small instruct model. The operator pulls the model (docs/inference.md).
DEFAULT_LOCAL_URL = "http://127.0.0.1:11434"
DEFAULT_LOCAL_MODEL = "llama3.1:8b"

# Bumped when what the gate does with a proposal changes, so old classifications go stale.
VALIDATION_VERSION = 2
# Bumped when the prompt or the request sent to a model changes, for the same reason.
INFERENCE_VERSION = 1


@dataclass(frozen=True)
class Endpoint:
    """An OpenAI-compatible chat completions server: its base URL and the model it is asked
    for. `key_file` holds the bearer token, for an external provider that needs one."""

    url: str | None
    model: str
    key_file: Path | None = None


@dataclass(frozen=True)
class ClassifierConfig:
    topics: tuple[Topic, ...] = ()
    deadline_range: DeadlineRange = DeadlineRange()
    # None means no classifier is configured: every item's classification is unavailable.
    backend: str | None = None
    profile: str = ""
    local: Endpoint = Endpoint(DEFAULT_LOCAL_URL, DEFAULT_LOCAL_MODEL)
    external: Endpoint | None = None
    # The accounts whose text goes to `external`. Every other account stays on `local`.
    external_accounts: frozenset[str] = frozenset()

    def route(self, account: str) -> str | None:
        """Where an account's items are classified: `local`, `external`, `stub`, or None."""
        if self.backend == "local" and account in self.external_accounts:
            return "external"
        return self.backend

    def digest(self, account: str) -> str:
        """Identifies everything a stored classification of this account depends on besides
        the item's text: the topic list, the deadline range and the account's inference
        settings. Another account's settings are not part of it."""
        range_ = self.deadline_range
        route = self.route(account)
        endpoint = {"local": self.local, "external": self.external}.get(route or "")
        inference: list[object] = [route, self.profile]
        if endpoint is not None:
            inference += [INFERENCE_VERSION, endpoint.url, endpoint.model]
        document = {
            "version": VALIDATION_VERSION,
            "topics": [[topic.id, topic.name, topic.description] for topic in self.topics],
            "deadline_range": [
                None if moment is None else minute(moment) for moment in (range_.start, range_.end)
            ],
            "inference": inference,
        }
        text = json.dumps(document, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(text.encode()).hexdigest()

    def resolved(self, local_url: str | None, external_url: str | None) -> "ClassifierConfig":
        """The configuration with the address of each inference stand-in applied, the way the
        throwaway and test stacks point the gate at them, checked as a whole.

        An address from the environment is the operator's own and is not held to the rules of
        one in the file (loopback for local, https or loopback for external).
        """
        config = self
        if local_url:
            config = replace(config, local=replace(config.local, url=_base(local_url)))
        if external_url and config.external is not None:
            config = replace(config, external=replace(config.external, url=_base(external_url)))
        if config.external_accounts and (config.external is None or config.external.url is None):
            raise ConfigError("inference.external needs a url for its accounts")
        return config


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
    inference = _object(
        section.get("inference", {}), {"backend", "profile", "local", "external"}, "inference"
    )
    backend = inference.get("backend", "local")
    if backend not in BACKENDS:
        raise ConfigError("inference.backend is not a known backend")
    profile = inference.get("profile", "")
    if not isinstance(profile, str) or (profile and not PROFILE_PATTERN.fullmatch(profile)):
        raise ConfigError("inference.profile must be 1 to 32 of a-z, 0-9, dot, dash, underscore")
    local = _local(inference.get("local", {}))
    external, accounts = _external(inference.get("external"))
    if backend != "local" and (external is not None or "local" in inference):
        raise ConfigError("inference.local and inference.external need the local backend")
    return ClassifierConfig(topics, range_, backend, profile, local, external, accounts)


def _local(value: object) -> Endpoint:
    fields = _object(value, {"url", "model"}, "inference.local")
    url = _url(fields.get("url", DEFAULT_LOCAL_URL), "inference.local.url", external=False)
    model = _model(fields.get("model", DEFAULT_LOCAL_MODEL), "inference.local.model")
    if model.lower().endswith("cloud"):
        # A loopback address is not proof of local execution: Ollama forwards a cloud-tagged
        # model (`name:cloud`, `name-cloud`) to its own servers.
        raise ConfigError("inference.local.model names a cloud model, which does not run here")
    return Endpoint(url, model)


def _external(value: object) -> tuple[Endpoint | None, frozenset[str]]:
    if value is None:
        return None, frozenset()
    fields = _object(value, {"url", "model", "api_key_file", "accounts"}, "inference.external")
    url = fields.get("url")
    if url is not None:
        url = _url(url, "inference.external.url", external=True)
    if "model" not in fields:
        raise ConfigError("inference.external.model is required")
    key_file = fields.get("api_key_file")
    if key_file is not None and (not isinstance(key_file, str) or not key_file):
        raise ConfigError("inference.external.api_key_file must be a file path")
    names = fields.get("accounts", [])
    if not isinstance(names, list) or not all(
        isinstance(name, str) and ACCOUNT_NAME.fullmatch(name) for name in names
    ):
        raise ConfigError("inference.external.accounts must be a list of account names")
    model = _model(fields["model"], "inference.external.model")
    endpoint = Endpoint(url, model, None if key_file is None else Path(key_file))
    return endpoint, frozenset(names)


def _model(value: object, what: str) -> str:
    if not isinstance(value, str) or MODEL_PATTERN.fullmatch(value) is None:
        raise ConfigError(f"{what} must be a model name such as llama3.1:8b")
    return value


def _url(value: object, what: str, *, external: bool) -> str:
    """A server's base URL. Local inference must be on this host; external may go elsewhere
    only over https."""
    try:
        if not isinstance(value, str) or len(value) > MAX_URL_CHARS:
            raise ValueError
        parts = urlsplit(value)
        port = parts.port
        problem = (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or port == 0
        )
        if problem:
            raise ValueError
    except ValueError:
        raise ConfigError(f"{what} must be an http(s) URL without credentials") from None
    if not _is_loopback(parts.hostname or ""):
        if not external:
            raise ConfigError(f"{what} must be on this host: localhost or a loopback address")
        if parts.scheme != "https":
            raise ConfigError(f"{what} must use https unless it is on this host")
    return _base(value)


def _base(url: str) -> str:
    """A server's base URL as the gate appends the API path to it."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError("an inference stand-in address must be an http(s) URL")
    return url.rstrip("/")


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
