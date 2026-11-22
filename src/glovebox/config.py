"""Service configuration, read once from the environment at startup."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_DB = "glovebox.db"


class ConfigError(Exception):
    """A setting is missing or unusable. The message never contains a setting's value."""


@dataclass(frozen=True)
class Settings:
    db_path: Path
    # Pins the clock the 30-day window is measured from. For tests and fixture stacks only:
    # real time is used when this is unset.
    fixed_now: datetime | None = None

    def clock(self) -> Callable[[], datetime]:
        fixed = self.fixed_now
        return (lambda: datetime.now(UTC)) if fixed is None else (lambda: fixed)

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        return cls(
            db_path=Path(env.get("GLOVEBOX_DB") or DEFAULT_DB),
            fixed_now=parse_fixed_now(env.get("GLOVEBOX_FIXED_NOW")),
        )


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
