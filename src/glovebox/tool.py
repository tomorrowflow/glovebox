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

from glovebox.access import (
    COUNT_OPERATION,
    GROUP_OPERATION,
    LIST_ACCOUNTS_OPERATION,
    LIST_OPERATION,
    Access,
    Grant,
)
from glovebox.credentials import CredentialsError
from glovebox.formats import MAX_ITEMS
from glovebox.output import (
    AccountsOutput,
    CountOutput,
    GroupOutput,
    ListOutput,
    Output,
    error_result,
    release,
    success_result,
)
from glovebox.query import (
    AccountsInput,
    CountInput,
    Filters,
    GroupInput,
    ListInput,
    decode_cursor,
    encode_cursor,
    query_digest,
)
from glovebox.sources import SourceError, open_source
from glovebox.store import Store

logger = logging.getLogger(__name__)

# One tool per read operation a token can be limited to, under the same name.
ACCOUNTS_TOOL = LIST_ACCOUNTS_OPERATION
LIST_TOOL = LIST_OPERATION
COUNT_TOOL = COUNT_OPERATION
GROUP_TOOL = GROUP_OPERATION

READ_ONLY = types.ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

FILTERS_TEXT = (
    "Filters, all optional and combined with AND: read status, received-time range "
    "(inclusive, minute precision), attachment presence, sender handle, thread handle "
    "and account handle."
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
    ACCOUNTS_TOOL: ToolSpec(
        _definition(
            ACCOUNTS_TOOL,
            "Lists the accounts this token covers: account handle and source kind for each. "
            "Accounts the token does not cover are not listed and cannot be told apart from "
            "accounts that do not exist.",
            AccountsInput,
            AccountsOutput,
        ),
        AccountsInput,
        AccountsOutput,
    ),
    LIST_TOOL: ToolSpec(
        _definition(
            LIST_TOOL,
            "Lists mail items of the last 30 days with permitted fields only: item handle, "
            "account handle, source kind, sender handle, thread handle, received time at "
            "minute precision, read status and attachment presence. "
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
            "sender handle, thread handle, read status or source kind. Largest groups first. "
            f"{FILTERS_TEXT} A count above 1000 is reported as 1000+. At most {MAX_ITEMS} "
            "groups per page; next_cursor is an opaque cursor for the next page, valid only "
            "for the same caller and query.",
            GroupInput,
            GroupOutput,
        ),
        GroupInput,
        GroupOutput,
    ),
}


class ReadTool:
    def __init__(self, store: Store, access: Access, clock: Callable[[], datetime]) -> None:
        self._store = store
        self._access = access
        self._clock = clock
        self._sync = threading.Lock()

    async def definitions(self, principal: str | None) -> list[types.Tool]:
        """The tools the caller's token may use, and no others."""
        grant = await self._grant(principal)
        if grant is None:
            return []
        return [spec.definition for name, spec in SPECS.items() if name in grant.operations]

    async def _grant(self, principal: str | None) -> Grant | None:
        # The database may be busy for a while: never wait for it on the event loop.
        if principal is None:
            return None
        return await anyio.to_thread.run_sync(self._access.grant, principal)

    async def call(
        self, name: str, arguments: Mapping[str, Any] | None, principal: str | None
    ) -> types.CallToolResult:
        """Answer one tool call. Whatever happens, the response passes the validator.

        `principal` is the id of the authenticated token; page cursors are bound to it, and
        so are the accounts and operations the call may reach.
        """
        output: type[Output] = Output
        try:
            grant = await self._grant(principal)
            spec = None if grant is None else _permitted_spec(name, grant.operations)
            if spec is not None:
                output = spec.output
            if grant is None or spec is None:
                # A tool outside the token's operations is answered as one that does not exist.
                response = error_result("unknown_tool" if grant else "internal_error")
            else:
                response = await anyio.to_thread.run_sync(
                    self._respond, name, spec, arguments, grant
                )
            return release(response, output)
        except Exception as error:
            # Only the type is logged: an exception message may hold upstream text.
            logger.error("tool call failed: %s", type(error).__name__)
            return release(error_result("internal_error", output))

    def _respond(
        self, name: str, spec: ToolSpec, arguments: Mapping[str, Any] | None, grant: Grant
    ) -> types.CallToolResult:
        try:
            request = spec.inputs.model_validate(arguments or {}, strict=True)
        except ValidationError:
            # The message of a validation error quotes the input, so it is dropped.
            return error_result("invalid_arguments", spec.output)
        if isinstance(request, AccountsInput):
            return success_result(
                {
                    "accounts": [
                        {"account_handle": account.handle, "source_kind": account.kind}
                        for account in grant.accounts
                    ]
                }
            )
        if not isinstance(request, Filters):
            return error_result("internal_error", spec.output)

        paging: Paging | None = None
        if isinstance(request, ListInput | GroupInput):
            selector = request.sort if isinstance(request, ListInput) else request.group_by
            digest = query_digest(name, request.only_filters(), selector)
            # Cursors are bound to the token: another token's cursor, for any account, is
            # as invalid as one that was never issued.
            paging = Paging(self._store.cursor_key(), grant.token_id, digest, 0)
            if request.cursor is not None:
                paging = paging.resume(request.cursor)
                if paging is None:
                    return error_result("invalid_cursor", spec.output)
        filters = request.only_filters()
        # An account handle outside the token's accounts selects nothing, exactly as one
        # that does not exist does. Only the selected accounts are fetched.
        selected = [
            account
            for account in grant.accounts
            if filters.account_handle in (None, account.handle)
        ]

        with self._sync:
            fetched = []
            for account in selected:
                try:
                    source = open_source(account.kind, account.source, account.credentials)
                    fetched.append((account, source.fetch()))
                except CredentialsError as error:
                    # The message is fixed text; the account name is the operator's own label.
                    logger.warning("account %s unavailable: %s", account.name, error)
                    return error_result("source_unavailable", spec.output)
                except SourceError:
                    logger.warning("account %s unavailable: its source failed", account.name)
                    return error_result("source_unavailable", spec.output)
            now = self._clock()
            for account, mailbox in fetched:
                self._store.replace_mailbox(account.handle, account.kind, mailbox, now)
            handles = [account.handle for account in selected]
            if isinstance(request, ListInput) and paging is not None:
                # One row past the page tells whether there is a next one.
                rows = self._store.list_items(
                    handles, now, filters, request.sort, request.limit + 1, paging.offset
                )
                items = rows[: request.limit]
                return success_result(
                    {"items": items, "next_cursor": paging.next(len(items), len(rows) > len(items))}
                )
            if isinstance(request, GroupInput) and paging is not None:
                found = self._store.group_items(
                    handles, now, filters, request.group_by, request.limit + 1, paging.offset
                )
                groups = found[: request.limit]
                return success_result(
                    {
                        "groups": groups,
                        "next_cursor": paging.next(len(groups), len(found) > len(groups)),
                    }
                )
            return success_result({"count": self._store.count_items(handles, now, filters)})


def _permitted_spec(name: str, operations: frozenset[str]) -> ToolSpec | None:
    return SPECS.get(name) if name in operations else None


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
