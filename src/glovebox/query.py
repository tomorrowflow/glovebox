"""Tool inputs and page cursors.

Inputs are the same closed kinds as outputs: enums, handles, minute timestamps,
booleans, a bounded number and a cursor. There is no text or semantic query
parameter, so nothing an agent sends can be a search string. The input models are
both the published input schemas and the rule every call is checked against.

A cursor is an offset plus a MAC over the caller, the query and that offset. It
reveals only a position, it cannot be forged, and it is invalid for any other token
identity or any other query.
"""

import base64
import hashlib
import hmac
import json
import struct
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from glovebox.fields import ClassificationStatus, Cursor, Handle, Minute, TopicId, YesNoUnknown
from glovebox.formats import CURSOR_LENGTH, MAX_ITEMS
from glovebox.sources import ReadStatus

SortOrder = Literal["received_desc", "received_asc", "unread_first", "attachments_first"]
GroupBy = Literal[
    "sender_handle",
    "thread_handle",
    "read_status",
    "source_kind",
    "topic",
    "reply_needed",
    "action_requested",
    "deadline",
    "classification_status",
]

OFFSET_BYTES = 4
MAC_BYTES = 16


class AccountsInput(BaseModel):
    """Listing accounts takes no input."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class Filters(BaseModel):
    """Every filter is optional; the ones given are combined with AND. Ranges are inclusive."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    read_status: ReadStatus | None = None
    received_from: Minute | None = None
    received_to: Minute | None = None
    has_attachments: bool | None = None
    sender_handle: Handle | None = None
    thread_handle: Handle | None = None
    account_handle: Handle | None = None
    # The semantic fields. A topic ID that is not configured matches nothing; a deadline
    # range matches only items whose deadline is known.
    topic: TopicId | None = None
    reply_needed: YesNoUnknown | None = None
    action_requested: YesNoUnknown | None = None
    deadline_from: Minute | None = None
    deadline_to: Minute | None = None
    classification_status: ClassificationStatus | None = None

    def only_filters(self) -> "Filters":
        """These filters alone, without the sort, page size or cursor of a subclass."""
        return Filters(**{name: getattr(self, name) for name in Filters.model_fields})


class ListInput(Filters):
    sort: SortOrder = "received_desc"
    limit: int = Field(default=MAX_ITEMS, ge=1, le=MAX_ITEMS)
    cursor: Cursor | None = None


class CountInput(Filters):
    pass


class TopicsInput(BaseModel):
    """`list_topics` takes no arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class GroupInput(Filters):
    group_by: GroupBy
    limit: int = Field(default=MAX_ITEMS, ge=1, le=MAX_ITEMS)
    cursor: Cursor | None = None


def query_digest(tool: str, filters: Filters, selector: str) -> bytes:
    """What a cursor is bound to: the tool, the filters and the sort order or grouping.

    The page size is left out on purpose: a client may change it between pages.
    """
    document = json.dumps(
        [tool, selector, filters.model_dump(mode="json")], sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(document.encode()).digest()


def _mac(key: bytes, principal: str, digest: bytes, offset: bytes) -> bytes:
    material = b"\0".join((principal.encode(), digest, offset))
    return hmac.new(key, material, hashlib.sha256).digest()[:MAC_BYTES]


def _spell(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def encode_cursor(key: bytes, principal: str, digest: bytes, offset: int) -> str:
    packed = struct.pack(">I", offset)
    return _spell(packed + _mac(key, principal, digest, packed))


def decode_cursor(key: bytes, principal: str, digest: bytes, cursor: str) -> int | None:
    """The offset a cursor stands for, or None if it is not valid for this caller and query."""
    if len(cursor) != CURSOR_LENGTH:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=")
    except ValueError:
        return None
    packed, mac = raw[:OFFSET_BYTES], raw[OFFSET_BYTES:]
    if not hmac.compare_digest(mac, _mac(key, principal, digest, packed)):
        return None
    # A cursor has one spelling: the last character carries two bits that decoding drops.
    if _spell(raw) != cursor:
        return None
    offset: int = struct.unpack(">I", packed)[0]
    return offset
