"""Upstream credentials: plain files that only the gateway's user may touch (ADR 0004).

There is no encryption and no key management on purpose. The protection is the file
mode and the host: a credentials file that is accessible to group or others is refused,
and its contents go only to the source adapter that needs them, never into a response,
an exception message or a log line.
"""

import os
import stat
from pathlib import Path

MAX_CREDENTIALS_BYTES = 64 * 1024


class CredentialsError(Exception):
    """The file cannot be used. The message is fixed text: it never holds file contents."""


class Secret:
    """Credential text that does not print itself."""

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret(<redacted>)"

    __str__ = __repr__


def read_credentials(path: Path) -> Secret:
    """The contents of a credentials file, if it is a regular file closed to group and others.

    The mode is read from the opened file, so nothing can be swapped in between the check
    and the read, and nothing is read before the check passes.
    """
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        raise CredentialsError("the credentials file cannot be opened") from None
    with os.fdopen(descriptor, "rb") as handle:
        status = os.fstat(handle.fileno())
        if not stat.S_ISREG(status.st_mode):
            raise CredentialsError("the credentials file is not a regular file")
        if status.st_mode & 0o077:
            raise CredentialsError("the credentials file is accessible to group or others")
        data = handle.read(MAX_CREDENTIALS_BYTES + 1)
    if len(data) > MAX_CREDENTIALS_BYTES:
        raise CredentialsError("the credentials file is too large")
    try:
        return Secret(data.decode("utf-8").strip())
    except UnicodeDecodeError:
        raise CredentialsError("the credentials file is not text") from None
