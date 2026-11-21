"""The permitted-field schema and the one output validator (ADR 0001).

`ToolOutput` is both the published output schema and the rule every response is
checked against, so what the tool declares is what it can release. No field is a
free string: each is an enum, a bool, a handle or a minute-precision timestamp, and
every object is closed. `release` checks the entire response and, if anything is
off, returns the fixed error instead; nothing of the failed response survives.
"""

from datetime import datetime
from typing import Annotated, Literal

from mcp import types
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
)

from glovebox.formats import (
    HANDLE_LENGTH,
    HANDLE_PATTERN,
    MINUTE_FORMAT,
    MINUTE_LENGTH,
    MINUTE_PATTERN,
)
from glovebox.sources import ReadStatus, SourceKind

MAX_ITEMS = 50

ErrorCode = Literal[
    "none",
    "invalid_arguments",
    "unknown_tool",
    "source_unavailable",
    "response_invalid",
    "internal_error",
]


def _is_a_minute(value: str) -> str:
    datetime.strptime(value, MINUTE_FORMAT)
    return value


Handle = Annotated[
    str,
    StringConstraints(pattern=HANDLE_PATTERN, min_length=HANDLE_LENGTH, max_length=HANDLE_LENGTH),
]
Minute = Annotated[
    str,
    StringConstraints(pattern=MINUTE_PATTERN, min_length=MINUTE_LENGTH, max_length=MINUTE_LENGTH),
    AfterValidator(_is_a_minute),
]


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


class ToolOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    error: ErrorCode
    items: list[MailItem] = Field(max_length=MAX_ITEMS)


def error_result(code: ErrorCode) -> types.CallToolResult:
    """The response for a failure: a server constant, never anything of the cause."""
    return types.CallToolResult(
        content=[], structured_content={"error": code, "items": []}, is_error=True
    )


def success_result(items: list[dict[str, object]]) -> types.CallToolResult:
    return types.CallToolResult(
        content=[], structured_content={"error": "none", "items": items}, is_error=False
    )


def release(result: types.CallToolResult) -> types.CallToolResult:
    """Return `result` if all of it is permitted, else the fixed `response_invalid` error."""
    return result if _is_permitted(result) else error_result("response_invalid")


def _is_permitted(result: object) -> bool:
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
        output = ToolOutput.model_validate(structured, strict=True)
        if output.model_dump(mode="json") != structured:
            return False
        failed = output.error != "none"
        return result.is_error == failed and (not failed or output.items == [])
    except (ValidationError, ValueError, TypeError):
        return False
