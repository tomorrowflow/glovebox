"""Service configuration, read once from the environment at startup."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

MIN_TOKEN_LENGTH = 16
TOKEN_PATTERN = re.compile(r"[\x21-\x7e]+")  # printable ASCII, no whitespace
DEFAULT_DB = "glovebox.db"


class ConfigError(Exception):
    """A setting is missing or unusable. The message never contains a setting's value."""


@dataclass(frozen=True)
class Settings:
    agent_token: str
    db_path: Path
    mail_url: str | None
    mail_fixture: Path | None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "Settings":
        token = env.get("GLOVEBOX_AGENT_TOKEN", "")
        if len(token) < MIN_TOKEN_LENGTH or not TOKEN_PATTERN.fullmatch(token):
            raise ConfigError(
                f"GLOVEBOX_AGENT_TOKEN must be at least {MIN_TOKEN_LENGTH} printable "
                "characters without whitespace"
            )
        mail_url = env.get("GLOVEBOX_STANDIN_MAILBOX_MAIL") or None
        mail_fixture = env.get("GLOVEBOX_MAIL_FIXTURE") or None
        if mail_url is not None and mail_fixture is not None:
            raise ConfigError(
                "set only one of GLOVEBOX_STANDIN_MAILBOX_MAIL and GLOVEBOX_MAIL_FIXTURE"
            )
        if mail_url is not None and urlsplit(mail_url).scheme not in ("http", "https"):
            raise ConfigError("GLOVEBOX_STANDIN_MAILBOX_MAIL must be an http or https URL")
        return cls(
            agent_token=token,
            db_path=Path(env.get("GLOVEBOX_DB") or DEFAULT_DB),
            mail_url=mail_url,
            mail_fixture=Path(mail_fixture) if mail_fixture else None,
        )
