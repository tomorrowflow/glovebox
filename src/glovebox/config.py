"""Service configuration, read once from the environment at startup."""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_DB = "glovebox.db"
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
    # Pins the clock the 30-day window is measured from. For tests and fixture stacks only:
    # real time is used when this is unset.
    fixed_now: datetime | None = None
    # Host header values served besides loopback, each listed in full. Empty by default.
    allowed_hosts: tuple[str, ...] = ()

    def clock(self) -> Callable[[], datetime]:
        fixed = self.fixed_now
        return (lambda: datetime.now(UTC)) if fixed is None else (lambda: fixed)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        return cls(
            db_path=Path(env.get("GLOVEBOX_DB") or DEFAULT_DB),
            fixed_now=parse_fixed_now(env.get("GLOVEBOX_FIXED_NOW")),
            allowed_hosts=parse_allowed_hosts(env.get("GLOVEBOX_ALLOWED_HOSTS")),
        )


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
