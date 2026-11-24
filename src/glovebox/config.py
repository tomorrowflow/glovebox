"""Service configuration, read once from the environment at startup."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_DB = "glovebox.db"
MAX_SILENCE_VAR = "GLOVEBOX_IMESSAGE_MAX_SILENCE_HOURS"
MAX_SILENCE_DIGITS = 6  # up to 999999 hours, far beyond any window
DEFAULT_IMESSAGE_MAX_SILENCE = timedelta(hours=72)
# One Host header value, spelled out: a DNS name or an IPv4 address, or a bracketed IPv6
# address, then optionally a port. No wildcard is expressible.
HOST_PATTERN = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?|\[[0-9a-f:]+\])(?::(?P<port>[0-9]{1,5}))?"
)


class ConfigError(Exception):
    """A setting is missing or unusable. The message never contains a setting's value."""


@dataclass(frozen=True)
class Settings:
    db_path: Path
    # The operator's topic list, deadline range and inference settings; read by the service
    # at startup. Unset means the defaults: an empty topic list and local inference.
    classifier_config: Path | None = None
    # Test-only controls of the stub classifier: its script, and a record of its calls.
    stub_script: Path | None = None
    stub_probe: Path | None = None
    # The addresses of the inference stand-ins, which the throwaway and test stacks set; they
    # replace the address in the classifier configuration. Unset on a real deployment.
    standin_inference_local: str | None = None
    standin_inference_external: str | None = None
    # Pins the clock the 30-day window is measured from. For tests and fixture stacks only:
    # real time is used when this is unset.
    fixed_now: datetime | None = None
    # Host header values served besides loopback, each listed in full. Empty by default.
    allowed_hosts: tuple[str, ...] = ()
    # How long the Mac's Messages database may show no activity at all before the iMessage
    # account is `stale`; None turns the check off. A database stays readable when Messages
    # stops syncing, so silence is the only sign of it the gate can see.
    imessage_max_silence: timedelta | None = DEFAULT_IMESSAGE_MAX_SILENCE

    def clock(self) -> Callable[[], datetime]:
        fixed = self.fixed_now
        return (lambda: datetime.now(UTC)) if fixed is None else (lambda: fixed)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        return cls(
            db_path=Path(env.get("GLOVEBOX_DB") or DEFAULT_DB),
            classifier_config=_path(env.get("GLOVEBOX_CLASSIFIER_CONFIG")),
            stub_script=_path(env.get("GLOVEBOX_STUB_CLASSIFIER_SCRIPT")),
            stub_probe=_path(env.get("GLOVEBOX_STUB_CLASSIFIER_PROBE")),
            standin_inference_local=env.get("GLOVEBOX_STANDIN_INFERENCE_LOCAL") or None,
            standin_inference_external=env.get("GLOVEBOX_STANDIN_INFERENCE_EXTERNAL") or None,
            fixed_now=parse_fixed_now(env.get("GLOVEBOX_FIXED_NOW")),
            allowed_hosts=parse_allowed_hosts(env.get("GLOVEBOX_ALLOWED_HOSTS")),
            imessage_max_silence=parse_max_silence(env.get(MAX_SILENCE_VAR)),
        )


def parse_max_silence(value: str | None) -> timedelta | None:
    """The silence limit in whole hours: unset is the default, 0 turns the check off."""
    if value is None or not value.strip():
        return DEFAULT_IMESSAGE_MAX_SILENCE
    text = value.strip()
    if not (text.isascii() and text.isdigit()) or len(text) > MAX_SILENCE_DIGITS:
        raise ConfigError(f"{MAX_SILENCE_VAR} must be a whole number of hours, 0 to turn it off")
    hours = int(text)
    return timedelta(hours=hours) if hours else None


def parse_allowed_hosts(value: str | None) -> tuple[str, ...]:
    """The extra Host header values to serve: comma-separated `host` or `host:port`, no wildcard."""
    if value is None or not value.strip():
        return ()
    hosts = tuple(entry.strip().lower() for entry in value.split(","))
    for host in hosts:
        match = HOST_PATTERN.fullmatch(host)
        if match is None or int(match["port"] or 0) > 65535:
            raise ConfigError(
                "GLOVEBOX_ALLOWED_HOSTS must list exact host names or host:port values,"
                " separated by commas, without wildcards"
            )
    return hosts


def _path(value: str | None) -> Path | None:
    return Path(value) if value else None


def parse_fixed_now(value: str | None) -> datetime | None:
    """The pinned clock, if one is set: a timezone-aware ISO 8601 time."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        raise ConfigError("GLOVEBOX_FIXED_NOW must be an ISO 8601 time with a UTC offset")
    return parsed.astimezone(UTC)
