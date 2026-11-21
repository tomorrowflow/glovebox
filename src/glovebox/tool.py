"""The read tool: static definition and the one path from a call to a response."""

import logging
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import anyio.to_thread
from mcp import types

from glovebox.output import MAX_ITEMS, ToolOutput, error_result, release, success_result
from glovebox.sources import MailSource, SourceError
from glovebox.store import Store

logger = logging.getLogger(__name__)

TOOL_NAME = "list_recent_mail"

# Static constants: nothing in a definition is derived from source data.
TOOL = types.Tool(
    name=TOOL_NAME,
    description=(
        "Lists the most recent mail items of the configured account, newest first. "
        "Each item has an item handle, account handle, source kind, sender handle, "
        "thread handle, received time at minute precision, read status and attachment "
        "presence. Handles are opaque. No message text is returned."
    ),
    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    output_schema=ToolOutput.model_json_schema(),
    annotations=types.ToolAnnotations(
        read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
    ),
)


class ReadTool:
    def __init__(self, store: Store, source: MailSource | None) -> None:
        self._store = store
        self._source = source
        self._sync = threading.Lock()

    async def call(self, name: str, arguments: Mapping[str, Any] | None) -> types.CallToolResult:
        """Answer one tool call. Whatever happens, the response passes the validator."""
        try:
            response = await anyio.to_thread.run_sync(self._respond, name, arguments)
            return release(response)
        except Exception as error:
            # Only the type is logged: an exception message may hold upstream text.
            logger.error("tool call failed: %s", type(error).__name__)
            return release(error_result("internal_error"))

    def _respond(self, name: str, arguments: Mapping[str, Any] | None) -> types.CallToolResult:
        if name != TOOL_NAME:
            return error_result("unknown_tool")
        if arguments:
            return error_result("invalid_arguments")
        if self._source is None:
            return error_result("source_unavailable")
        with self._sync:
            try:
                mailbox = self._source.fetch()
            except SourceError:
                logger.warning("source unavailable")
                return error_result("source_unavailable")
            account = self._store.replace_mailbox(self._source.kind, mailbox, datetime.now(UTC))
            return success_result(self._store.recent_items(account, MAX_ITEMS))
