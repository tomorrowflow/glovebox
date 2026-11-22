"""Read received iMessages from the Messages database (`~/Library/Messages/chat.db`).

The database is the Mac's own history, written by Messages. The gate opens it read-only
and takes only what an item needs: the message's guid, the sender's address, the
conversation's guid, the time, the read flag, whether it has attachments and its text, which
goes to the quarantined classifier and nowhere else. Contact and group names, display names,
attachment names and types, reactions and every other column are never selected. Nothing is
copied: the rows are read where they are, so the gate keeps no archive of them (ADR 0004).

Nothing here may change the database. The connection is opened `mode=ro`, set `query_only`
and given an authorizer that refuses everything except reading, so the gate could not write
even if a query tried to. That is gateway-enforced read-only: the macOS permission that lets
the process open the file (Full Disk Access) does not distinguish reading from writing.

This module raises its own errors, with no detail on purpose: sqlite error text can quote
the database.
"""

import os
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
# Current macOS stores a message's date as nanoseconds since 2001-01-01 UTC; older ones used
# seconds. No date in seconds reaches this for several thousand years.
NANOSECOND_DATES = 100_000_000_000
# The most messages one read returns. A window holding more is cut to the newest, and the
# rest is reported as skipped, so that the account is `incomplete`, not `current`.
MAX_MESSAGES = 50_000
BUSY_TIMEOUT_SECONDS = 5
# Reactions (tapbacks) are messages of these types; they are not messages to triage.
FIRST_REACTION_TYPE, LAST_REACTION_TYPE = 2000, 3999

SECONDS_OF_DATE = (
    f"case when m.date > {NANOSECOND_DATES} then m.date / 1000000000.0 else m.date end"
)
# One statement, so that the total of the window and the rows come from the same snapshot:
# `count(*) over ()` counts every row of the window, before the limit cuts it.
QUERY = f"""
select m.guid, h.id,
  (select c.guid from chat c where c.ROWID =
    (select min(j.chat_id) from chat_message_join j where j.message_id = m.ROWID)),
  m.date, m.is_read, m.cache_has_attachments, m.text, m.attributedBody,
  count(*) over ()
from message m
left join handle h on h.ROWID = m.handle_id
where coalesce(m.is_from_me, 0) = 0
  and coalesce(m.item_type, 0) = 0
  and coalesce(m.associated_message_type, 0)
    not between {FIRST_REACTION_TYPE} and {LAST_REACTION_TYPE}
  and {SECONDS_OF_DATE} >= ?
order by m.date desc
limit ?
"""

_ALLOWED_ACTIONS = frozenset(
    {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
)


class ChatDbError(Exception):
    """The database could not be read or understood."""


class ChatDbBlocked(ChatDbError):
    """The database is missing or this process may not open it: Messages is not set up for
    this user, or the permission to read it is not granted."""


@dataclass(frozen=True)
class ChatMessage:
    guid: str
    sender: str
    chat: str
    received: datetime  # UTC
    is_read: bool | None
    has_attachments: bool
    text: str = field(default="", repr=False)


@dataclass(frozen=True)
class ChatMessages:
    messages: list[ChatMessage]
    skipped: int  # messages that could not be understood, or did not fit under the cap


def _authorize(action: int, *_: object) -> int:
    return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY


@contextmanager
def open_read_only(path: Path) -> Iterator[sqlite3.Connection]:
    """A connection that can only read. Raises ChatDbBlocked if the file cannot be opened."""
    try:
        os.close(os.open(path, os.O_RDONLY))
    except (PermissionError, FileNotFoundError):
        raise ChatDbBlocked from None
    except OSError:
        raise ChatDbError from None
    try:
        with closing(
            sqlite3.connect(
                f"{Path(path).absolute().as_uri()}?mode=ro",
                uri=True,
                timeout=BUSY_TIMEOUT_SECONDS,
                isolation_level=None,
            )
        ) as database:
            database.execute("pragma query_only = on")
            database.execute("pragma temp_store = memory")  # nothing spills to disk
            database.set_authorizer(_authorize)
            yield database
    except sqlite3.Error:
        raise ChatDbError from None


def read_messages(path: Path, since: datetime) -> ChatMessages:
    """The received messages at or after `since` (UTC), newest first, at most MAX_MESSAGES."""
    seconds = (since - APPLE_EPOCH).total_seconds()
    messages: list[ChatMessage] = []
    skipped = 0
    with open_read_only(path) as database:
        rows = database.execute(QUERY, (seconds, MAX_MESSAGES)).fetchall()
    total = int(rows[0][-1]) if rows else 0
    for row in rows:
        message = _message_of(row[:-1])
        if message is None:
            skipped += 1
        else:
            messages.append(message)
    return ChatMessages(messages, skipped + max(0, total - len(rows)))


def _message_of(row: tuple[object, ...]) -> ChatMessage | None:
    guid, sender, chat, date, is_read, attachments, text, body = row
    received = _time_of(date)
    if (
        not isinstance(guid, str)
        or not guid
        or not isinstance(sender, str)
        or not sender
        or not isinstance(chat, str)
        or not chat
        or received is None
    ):
        return None
    if not isinstance(text, str) or not text:
        text = text_of_body(body) if isinstance(body, bytes) else ""
    return ChatMessage(
        guid=guid,
        sender=sender,
        chat=chat,
        received=received,
        is_read=bool(is_read) if isinstance(is_read, int) else None,
        has_attachments=bool(attachments) if isinstance(attachments, int) else False,
        # U+FFFC stands for an attachment inside the text; it is not text.
        text=text.replace("￼", ""),
    )


def _time_of(date: object) -> datetime | None:
    if not isinstance(date, int | float) or isinstance(date, bool):
        return None
    seconds = date / 1_000_000_000 if date > NANOSECOND_DATES else date
    try:
        return APPLE_EPOCH + timedelta(seconds=seconds)
    except (OverflowError, ValueError):
        return None


# What precedes a string in the typed stream Messages keeps in `attributedBody` when it
# leaves `text` empty (macOS Ventura and later): the class name, five bytes, then a length.
_STRING_CLASS = b"NSString"
_STRING_PREFIX = b"\x01\x94\x84\x01+"


def text_of_body(body: bytes) -> str:
    """The message text in an `attributedBody` blob, or "" if it is not found.

    Only the string is read, never its attributes, which can name links, mentions and
    attachments. A blob that does not look like the known layout is no text, not an error.
    """
    start = body.find(_STRING_CLASS)
    if start < 0:
        return ""
    position = start + len(_STRING_CLASS)
    if body[position : position + len(_STRING_PREFIX)] != _STRING_PREFIX:
        return ""
    position += len(_STRING_PREFIX)
    if position >= len(body):
        return ""
    marker = body[position]
    position += 1
    if marker == 0x81:  # a two-byte length follows
        width = 2
    elif marker == 0x82:  # a four-byte length follows
        width = 4
    else:
        width = 0
    if width:
        length = int.from_bytes(body[position : position + width], "little")
        position += width
    else:
        length = marker
    if position + length > len(body):
        return ""
    return body[position : position + length].decode("utf-8", "replace")
