"""The permitted-field schemas and the one output validator (ADR 0001).

Each output model is both the published output schema of a tool and the rule every
response of that tool is checked against, so what the tool declares is what it can
release. No field is a free string: each is an enum, a bool, a handle, a count, a
cursor or a minute-precision timestamp, and every object is closed. `release` checks
the entire response and, if anything is off, returns the fixed error instead; nothing
of the failed response survives.
"""

from collections.abc import Mapping, Sequence
from typing import Literal

from mcp import types
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from glovebox.fields import Count, Cursor, Handle, Minute, SkippedCount
from glovebox.formats import MAX_ITEMS
from glovebox.sources import ReadStatus, SourceKind, SyncStatus

ErrorCode = Literal[
    "none",
    "invalid_arguments",
    "invalid_cursor",
    "unknown_tool",
    "source_unavailable",
    "response_invalid",
    "internal_error",
]

# What a group is keyed by: a handle, or a value of one of the enum fields.
GroupKey = Handle | ReadStatus | SourceKind


class Output(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error: ErrorCode


class MailItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_handle: Handle
    account_handle: Handle
    source_kind: SourceKind
    sender_handle: Handle
    thread_handle: Handle
    received: Minute
    read_status: ReadStatus
    has_attachments: bool


class AccountSync(BaseModel):
    """How fresh one account's data is: a status, the last sync that read the source, and
    how many items of it could not be understood."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    account_handle: Handle
    source_kind: SourceKind
    status: SyncStatus
    last_sync: Minute
    skipped_items: SkippedCount


class ReadOutput(Output):
    """What every read tool says about the accounts behind its answer, so that an outage
    or a stale account is never mistaken for an empty one."""

    accounts: list[AccountSync] = Field(default=[], max_length=MAX_ITEMS)


class ListOutput(ReadOutput):
    items: list[MailItem] = Field(default=[], max_length=MAX_ITEMS)
    next_cursor: Cursor | None = None


class CountOutput(ReadOutput):
    count: Count = "0"


class Group(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: GroupKey
    count: Count


class GroupOutput(ReadOutput):
    groups: list[Group] = Field(default=[], max_length=MAX_ITEMS)
    next_cursor: Cursor | None = None


def error_result(
    code: ErrorCode,
    model: type[Output] = Output,
    accounts: Sequence[Mapping[str, object]] = (),
) -> types.CallToolResult:
    """The response for a failure: a server constant, never anything of the cause.

    `model` shapes the empty payload so that it fits the tool's output schema. Only a
    source outage carries `accounts`, the freshness of what the gate last served; they
    are checked by `release` like everything else.
    """
    structured = model(error=code).model_dump(mode="json")
    if accounts:
        structured["accounts"] = [dict(account) for account in accounts]
    return types.CallToolResult(content=[], structured_content=structured, is_error=True)


def success_result(payload: dict[str, object]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[], structured_content={"error": "none", **payload}, is_error=False
    )


def release(result: types.CallToolResult, model: type[Output] = Output) -> types.CallToolResult:
    """Return `result` if all of it is permitted, else the fixed `response_invalid` error."""
    return result if _is_permitted(result, model) else error_result("response_invalid", model)


def _is_permitted(result: object, model: type[Output]) -> bool:
    try:
        if type(result) is not types.CallToolResult:
            return False
        # Every channel of the result, not only the structured content: no content
        # blocks, no metadata, no field the schema does not name.
        if result.content != [] or result.meta is not None or result.model_extra:
            return False
        if result.result_type != "complete":
            return False
        structured = result.structured_content
        output = model.model_validate(structured, strict=True)
        if output.model_dump(mode="json") != structured:
            return False
        failed = output.error != "none"
        if result.is_error != failed:
            return False
        if not failed:
            return True
        # A failure carries nothing but its code, in the shape of the tool's schema. A
        # source outage also carries the freshness of the accounts it affects.
        if not isinstance(structured, dict):
            return False
        bare = model(error=output.error).model_dump(mode="json")
        if isinstance(output, ReadOutput) and output.error == "source_unavailable":
            return {**structured, "accounts": []} == bare
        return structured == bare
    except (ValidationError, ValueError, TypeError):
        return False
