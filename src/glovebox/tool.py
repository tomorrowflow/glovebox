"""The read tools: static definitions and the one path from a call to a response."""

import logging
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import anyio.to_thread
from mcp import types
from pydantic import BaseModel, ValidationError

from glovebox.classification import QuarantinedClassifier
from glovebox.classifier_config import ClassifierConfig
from glovebox.formats import MAX_ITEMS
from glovebox.output import (
    CountOutput,
    GroupOutput,
    ListOutput,
    Output,
    TopicsOutput,
    error_result,
    release,
    success_result,
)
from glovebox.query import (
    CountInput,
    Filters,
    GroupInput,
    ListInput,
    TopicsInput,
    decode_cursor,
    encode_cursor,
    query_digest,
)
from glovebox.sources import MailSource, SourceError
from glovebox.store import Store

logger = logging.getLogger(__name__)

LIST_TOOL = "list_items"
COUNT_TOOL = "count_items"
GROUP_TOOL = "group_items"
TOPICS_TOOL = "list_topics"

READ_ONLY = types.ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

FILTERS_TEXT = (
    "Filters, all optional and combined with AND: read status, received-time range "
    "(inclusive, minute precision), attachment presence, sender handle, thread handle, "
    "account handle and the semantic fields: topic ID, reply needed, action requested, "
    "deadline range and classification status. Semantic fields are untrusted judgments "
    "of a quarantined classifier, `unknown` whenever the gate cannot validate one, and "
    "never an authorization for an action."
)


def _definition(
    name: str, description: str, inputs: type[BaseModel], output: type[Output]
) -> types.Tool:
    # Static constants: nothing in a definition is derived from source data.
    return types.Tool(
        name=name,
        description=description,
        input_schema=inputs.model_json_schema(),
        output_schema=output.model_json_schema(),
        annotations=READ_ONLY,
    )


@dataclass(frozen=True)
class ToolSpec:
    definition: types.Tool
    inputs: type[BaseModel]
    output: type[Output]


SPECS: dict[str, ToolSpec] = {
    LIST_TOOL: ToolSpec(
        _definition(
            LIST_TOOL,
            "Lists mail items of the last 30 days with permitted fields only: item handle, "
            "account handle, source kind, sender handle, thread handle, received time at "
            "minute precision, read status, attachment presence and the semantic fields "
            "topic, reply needed, action requested, deadline and classification status. "
            f"{FILTERS_TEXT} Fixed sort orders only: received_desc, received_asc, "
            f"unread_first, attachments_first. At most {MAX_ITEMS} items per page; "
            "next_cursor is an opaque cursor for the next page, valid only for the same "
            "caller and query. No message text is returned.",
            ListInput,
            ListOutput,
        ),
        ListInput,
        ListOutput,
    ),
    COUNT_TOOL: ToolSpec(
        _definition(
            COUNT_TOOL,
            f"Counts the mail items of the last 30 days that match the filters. {FILTERS_TEXT} "
            "A count above 1000 is reported as 1000+.",
            CountInput,
            CountOutput,
        ),
        CountInput,
        CountOutput,
    ),
    GROUP_TOOL: ToolSpec(
        _definition(
            GROUP_TOOL,
            "Counts the mail items of the last 30 days that match the filters, grouped by "
            "sender handle, thread handle, read status, source kind, topic, reply needed, "
            "action requested, deadline or classification status. Largest groups first. "
            f"{FILTERS_TEXT} A count above 1000 is reported as 1000+. At most {MAX_ITEMS} "
            "groups per page; next_cursor is an opaque cursor for the next page, valid only "
            "for the same caller and query.",
            GroupInput,
            GroupOutput,
        ),
        GroupInput,
        GroupOutput,
    ),
    TOPICS_TOOL: ToolSpec(
        _definition(
            TOPICS_TOOL,
            "Lists the configured topic IDs, the vocabulary of the topic filter "
            "and group. Besides these, an item's topic can be `other` or `unknown`. Topic "
            "names and descriptions are never returned.",
            TopicsInput,
            TopicsOutput,
        ),
        TopicsInput,
        TopicsOutput,
    ),
}

TOOLS = [spec.definition for spec in SPECS.values()]


class ReadTool:
    def __init__(
        self,
        store: Store,
        source: MailSource | None,
        clock: Callable[[], datetime],
        classifier: QuarantinedClassifier,
        config: ClassifierConfig,
    ) -> None:
        self._store = store
        self._source = source
        self._clock = clock
        self._classifier = classifier
        self._topic_ids = [topic.id for topic in config.topics]
        self._config_digest = config.digest()
        self._sync = threading.Lock()

    async def call(
        self, name: str, arguments: Mapping[str, Any] | None, principal: str | None
    ) -> types.CallToolResult:
        """Answer one tool call. Whatever happens, the response passes the validator.

        `principal` is the identity of the authenticated caller; page cursors are bound to it.
        """
        spec = SPECS.get(name)
        output = spec.output if spec else Output
        try:
            response = await anyio.to_thread.run_sync(self._respond, name, arguments, principal)
            return release(response, output)
        except Exception as error:
            # Only the type is logged: an exception message may hold upstream text.
            logger.error("tool call failed: %s", type(error).__name__)
            return release(error_result("internal_error", output))

    def _respond(
        self, name: str, arguments: Mapping[str, Any] | None, principal: str | None
    ) -> types.CallToolResult:
        spec = SPECS.get(name)
        if spec is None:
            return error_result("unknown_tool")
        if principal is None:
            return error_result("internal_error", spec.output)
        try:
            request = spec.inputs.model_validate(arguments or {}, strict=True)
        except ValidationError:
            # The message of a validation error quotes the input, so it is dropped.
            return error_result("invalid_arguments", spec.output)
        if isinstance(request, TopicsInput):
            return success_result({"topics": self._topic_ids})
        if not isinstance(request, Filters):
            return error_result("internal_error", spec.output)

        paging: Paging | None = None
        if isinstance(request, ListInput | GroupInput):
            selector = request.sort if isinstance(request, ListInput) else request.group_by
            digest = query_digest(name, request.only_filters(), selector)
            paging = Paging(self._store.cursor_key(), principal, digest, 0)
            if request.cursor is not None:
                paging = paging.resume(request.cursor)
                if paging is None:
                    return error_result("invalid_cursor", spec.output)
        if self._source is None:
            return error_result("source_unavailable", spec.output)

        with self._sync:
            try:
                mailbox = self._source.fetch()
            except SourceError:
                logger.warning("source unavailable")
                return error_result("source_unavailable", spec.output)
            now = self._clock()
            # Each item is classified on its own, outside any database transaction; one
            # that is unchanged since its last classification is not classified again.
            prepared = self._store.prepare_mailbox(mailbox, now, self._config_digest)
            classifications = [
                item.cached or self._classifier.classify(item.item.text, now)
                for item in prepared.items
            ]
            account = self._store.commit_mailbox(
                self._source.kind, prepared, classifications, self._config_digest, now
            )
            filters = request.only_filters()
            if isinstance(request, ListInput) and paging is not None:
                # One row past the page tells whether there is a next one.
                rows = self._store.list_items(
                    account, now, filters, request.sort, request.limit + 1, paging.offset
                )
                items = rows[: request.limit]
                return success_result(
                    {"items": items, "next_cursor": paging.next(len(items), len(rows) > len(items))}
                )
            if isinstance(request, GroupInput) and paging is not None:
                found = self._store.group_items(
                    account, now, filters, request.group_by, request.limit + 1, paging.offset
                )
                groups = found[: request.limit]
                return success_result(
                    {
                        "groups": groups,
                        "next_cursor": paging.next(len(groups), len(found) > len(groups)),
                    }
                )
            return success_result({"count": self._store.count_items(account, now, filters)})


@dataclass(frozen=True)
class Paging:
    """Where a page starts, and what is needed to sign the cursor of the next one."""

    key: bytes
    principal: str
    digest: bytes
    offset: int

    def resume(self, cursor: str) -> "Paging | None":
        """The paging a cursor stands for, or None if it is not valid for this caller and query."""
        offset = decode_cursor(self.key, self.principal, self.digest, cursor)
        return None if offset is None else replace(self, offset=offset)

    def next(self, returned: int, more: bool) -> str | None:
        if not more:
            return None
        return encode_cursor(self.key, self.principal, self.digest, self.offset + returned)
