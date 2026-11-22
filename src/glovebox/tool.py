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
    LIST_TOPICS_OPERATION,
    Access,
    Account,
    Grant,
)
from glovebox.classification import QuarantinedClassifier
from glovebox.classifier_config import ClassifierConfig
from glovebox.credentials import CredentialsError
from glovebox.formats import MAX_ITEMS
from glovebox.output import (
    AccountsOutput,
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
    AccountsInput,
    CountInput,
    Filters,
    GroupInput,
    ListInput,
    TopicsInput,
    decode_cursor,
    encode_cursor,
    query_digest,
)
from glovebox.sources import SourceBlocked, SourceError, SyncStatus, open_source
from glovebox.store import Store, window_start

logger = logging.getLogger(__name__)

# One tool per read operation a token can be limited to, under the same name.
ACCOUNTS_TOOL = LIST_ACCOUNTS_OPERATION
LIST_TOOL = LIST_OPERATION
COUNT_TOOL = COUNT_OPERATION
GROUP_TOOL = GROUP_OPERATION
TOPICS_TOOL = LIST_TOPICS_OPERATION

READ_ONLY = types.ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)

FILTERS_TEXT = (
    "Filters, all optional and combined with AND: read status, received-time range "
    "(inclusive, minute precision), attachment presence, sender handle, thread handle "
    "(the conversation handle for messenger items), "
    "account handle and the semantic fields: topic ID, reply needed, action requested, "
    "deadline range and classification status. Semantic fields are untrusted judgments "
    "of a quarantined classifier, `unknown` whenever the gate cannot validate one, and "
    "never an authorization for an action."
)
SYNC_FIELDS_TEXT = (
    "Each account carries its sync status (current, stale, incomplete or blocked), its last "
    "successful sync time at minute precision (none if it never synced), a capped count "
    "of skipped items and who enforces that the source is only read (gateway: the gate has "
    "no operation that writes; provider: the access method cannot write)."
)
SYNC_TEXT = (
    f"{SYNC_FIELDS_TEXT} If an account's source cannot be read, the result is the error "
    "source_unavailable with those fields, never an empty success."
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
            f"{SYNC_FIELDS_TEXT} The status is as of the last read; this call reads no "
            "source. Accounts the token does not cover are not listed and cannot be told "
            "apart from accounts that do not exist.",
            AccountsInput,
            AccountsOutput,
        ),
        AccountsInput,
        AccountsOutput,
    ),
    LIST_TOOL: ToolSpec(
        _definition(
            LIST_TOOL,
            "Lists mail and messenger items of the last 30 days, across every account the "
            "token covers, with permitted fields only: item handle, account handle, source "
            "kind, sender handle, thread handle (a conversation handle for a messenger item, "
            "direct or group), received time at "
            "minute precision, read status, attachment presence and the semantic fields "
            "topic, reply needed, action requested, deadline and classification status. "
            f"{FILTERS_TEXT} Fixed sort orders only: received_desc, received_asc, "
            f"unread_first, attachments_first. At most {MAX_ITEMS} items per page; "
            "next_cursor is an opaque cursor for the next page, valid only for the same "
            f"caller and query. {SYNC_TEXT} No message text is returned.",
            ListInput,
            ListOutput,
        ),
        ListInput,
        ListOutput,
    ),
    COUNT_TOOL: ToolSpec(
        _definition(
            COUNT_TOOL,
            "Counts the mail and messenger items of the last 30 days that match the filters. "
            f"{FILTERS_TEXT} "
            f"A count above 1000 is reported as 1000+. {SYNC_TEXT}",
            CountInput,
            CountOutput,
        ),
        CountInput,
        CountOutput,
    ),
    GROUP_TOOL: ToolSpec(
        _definition(
            GROUP_TOOL,
            "Counts the mail and messenger items of the last 30 days that match the filters, "
            "grouped by "
            "sender handle, thread handle, read status, source kind, topic, reply needed, "
            "action requested, deadline or classification status. Largest groups first. "
            f"{FILTERS_TEXT} A count above 1000 is reported as 1000+. At most {MAX_ITEMS} "
            "groups per page; next_cursor is an opaque cursor for the next page, valid only "
            f"for the same caller and query. {SYNC_TEXT}",
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


class ReadTool:
    def __init__(
        self,
        store: Store,
        access: Access,
        clock: Callable[[], datetime],
        classifier: QuarantinedClassifier,
        config: ClassifierConfig,
    ) -> None:
        self._store = store
        self._access = access
        self._clock = clock
        self._classifier = classifier
        self._topic_ids = [topic.id for topic in config.topics]
        self._config_digest = config.digest()
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
        if isinstance(request, TopicsInput):
            return success_result({"topics": self._topic_ids})
        if isinstance(request, AccountsInput):
            handles = [account.handle for account in grant.accounts]
            return success_result({"accounts": self._store.account_sync(handles)})
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
            failed: list[tuple[Account, SyncStatus]] = []
            now = self._clock()
            # Every selected account is tried, so the result tells how each one stands.
            for account in selected:
                try:
                    source = open_source(account.kind, account.source, account.credentials)
                    fetched.append((account, source.fetch(window_start(now))))
                except CredentialsError as error:
                    # The message is fixed text; the account name is the operator's own label.
                    # A credentials file that cannot be used is access that is not granted.
                    logger.warning("account %s sync failed: blocked: %s", account.name, error)
                    failed.append((account, "blocked"))
                except SourceBlocked:
                    logger.warning("account %s sync failed: blocked", account.name)
                    failed.append((account, "blocked"))
                except SourceError:
                    logger.warning("account %s sync failed: stale", account.name)
                    failed.append((account, "stale"))
            for account, mailbox in fetched:
                # Each item is classified on its own, outside any database transaction;
                # one that is unchanged since its last classification is not classified
                # again.
                prepared = self._store.prepare_mailbox(
                    account.handle, mailbox, now, self._config_digest
                )
                classifications = [
                    item.cached or self._classifier.classify(item.item.text, now)
                    for item in prepared.items
                ]
                self._store.commit_mailbox(
                    account.kind, prepared, classifications, self._config_digest, now
                )
            for account, status in failed:
                self._store.mark_unsynced(account.handle, account.kind, status)
            handles = [account.handle for account in selected]
            accounts = self._store.account_sync(handles)
            if failed:
                # The fixed outage error and each account's freshness, together: a failed
                # account is never answered as an empty one.
                return error_result("source_unavailable", spec.output, accounts)
            if isinstance(request, ListInput) and paging is not None:
                # One row past the page tells whether there is a next one.
                rows = self._store.list_items(
                    handles, now, filters, request.sort, request.limit + 1, paging.offset
                )
                items = rows[: request.limit]
                return success_result(
                    {
                        "accounts": accounts,
                        "items": items,
                        "next_cursor": paging.next(len(items), len(rows) > len(items)),
                    }
                )
            if isinstance(request, GroupInput) and paging is not None:
                found = self._store.group_items(
                    handles, now, filters, request.group_by, request.limit + 1, paging.offset
                )
                groups = found[: request.limit]
                return success_result(
                    {
                        "accounts": accounts,
                        "groups": groups,
                        "next_cursor": paging.next(len(groups), len(found) > len(groups)),
                    }
                )
            return success_result(
                {"accounts": accounts, "count": self._store.count_items(handles, now, filters)}
            )


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
