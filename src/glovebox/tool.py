"""The read tools: static definitions and the one path from a call to a response."""

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

import anyio.to_thread
from mcp import types
from pydantic import BaseModel, ValidationError

from glovebox.access import (
    AVAILABILITY_OPERATION,
    COUNT_OPERATION,
    GROUP_OPERATION,
    LIST_ACCOUNTS_OPERATION,
    LIST_EVENTS_OPERATION,
    LIST_OPERATION,
    LIST_OVERLAPS_OPERATION,
    LIST_TOPICS_OPERATION,
    Access,
    Account,
    Grant,
)
from glovebox.availability import look_ahead_range, overlapping, windows
from glovebox.calendars import open_calendar
from glovebox.classification import QuarantinedClassifier
from glovebox.classifier_config import ClassifierConfig
from glovebox.credentials import CredentialsError
from glovebox.formats import MAX_ITEMS
from glovebox.output import (
    AccountsOutput,
    AvailabilityOutput,
    CountOutput,
    EventsOutput,
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
    EventsInput,
    Filters,
    GroupInput,
    ListInput,
    RangeFilters,
    RangeInput,
    TopicsInput,
    decode_cursor,
    encode_cursor,
    query_digest,
)
from glovebox.sources import (
    CALENDAR_KINDS,
    SourceBlocked,
    SourceError,
    SyncStatus,
    open_source,
)
from glovebox.store import Store, window_start

logger = logging.getLogger(__name__)

# One tool per read operation a token can be limited to, under the same name.
ACCOUNTS_TOOL = LIST_ACCOUNTS_OPERATION
LIST_TOOL = LIST_OPERATION
COUNT_TOOL = COUNT_OPERATION
GROUP_TOOL = GROUP_OPERATION
TOPICS_TOOL = LIST_TOPICS_OPERATION
AVAILABILITY_TOOL = AVAILABILITY_OPERATION
EVENTS_TOOL = LIST_EVENTS_OPERATION
OVERLAPS_TOOL = LIST_OVERLAPS_OPERATION

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
CALENDAR_TEXT = (
    "Covers the calendar accounts the token covers, or the one named by the account handle "
    "filter, for the next 90 days: events outside that look-ahead are not served, and one "
    "that runs past its end is cut there. All times are UTC at minute precision. An event "
    "holds time when its busy status is busy, tentative or unknown, and is free time when "
    "it is free (cancelled, declined or marked as free). A range is a range_from and a "
    "range_to (minute timestamps, either may be left out) in which an event overlaps; one "
    "that only touches it does not. At most "
    f"{MAX_ITEMS} entries per page; next_cursor is an opaque cursor for the next page, "
    f"valid only for the same caller and query. {SYNC_TEXT} No event text is returned: no "
    "title, description, location, attendee or organizer."
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
    AVAILABILITY_TOOL: ToolSpec(
        _definition(
            AVAILABILITY_TOOL,
            "Lists when the calendar owner is busy and when free in a time range, across the "
            "calendar accounts: consecutive windows that cover the range, each busy or free, "
            "earliest first, with events that run into each other or touch making one busy "
            f"window. {CALENDAR_TEXT}",
            RangeInput,
            AvailabilityOutput,
        ),
        RangeInput,
        AvailabilityOutput,
    ),
    EVENTS_TOOL: ToolSpec(
        _definition(
            EVENTS_TOOL,
            "Lists upcoming calendar events with permitted fields only: event handle, "
            "account handle, start, end, all-day flag and busy status (busy, tentative, free "
            "or unknown; a status the source gave that the gate does not know is unknown). "
            "Optional filters, combined with AND: range, account handle, busy status and "
            f"all-day flag. Fixed sort orders only: start_asc, start_desc. {CALENDAR_TEXT}",
            EventsInput,
            EventsOutput,
        ),
        EventsInput,
        EventsOutput,
    ),
    OVERLAPS_TOOL: ToolSpec(
        _definition(
            OVERLAPS_TOOL,
            "Lists the events that overlap another event, in a time range and across the "
            "calendar accounts, earliest first, with the same fields as list_events. Only "
            "events that hold time are compared: free ones overlap nothing, and events that "
            f"only touch do not overlap. {CALENDAR_TEXT}",
            RangeInput,
            EventsOutput,
        ),
        RangeInput,
        EventsOutput,
    ),
}


class ReadTool:
    def __init__(
        self,
        store: Store,
        access: Access,
        clock: Callable[[], datetime],
        classifier_for: Callable[[str], QuarantinedClassifier],
        config: ClassifierConfig,
        imessage_max_silence: timedelta | None = None,
    ) -> None:
        self._imessage_max_silence = imessage_max_silence
        self._store = store
        self._access = access
        self._clock = clock
        self._classifier_for = classifier_for
        self._topic_ids = [topic.id for topic in config.topics]
        self._config = config
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
        if isinstance(request, RangeFilters):
            return self._respond_calendar(name, spec, request, grant)
        if not isinstance(request, Filters):
            return error_result("internal_error", spec.output)

        paging: Paging | None = None
        if isinstance(request, ListInput | GroupInput):
            selector = request.sort if isinstance(request, ListInput) else request.group_by
            paging = self._paging(name, request.only_filters(), selector, request.cursor, grant)
            if paging is None:
                return error_result("invalid_cursor", spec.output)
        filters = request.only_filters()
        # An account handle outside the token's accounts selects nothing, exactly as one
        # that does not exist does, and so does the handle of a calendar account. Only the
        # selected accounts are fetched.
        selected = [
            account
            for account in grant.accounts
            if account.kind not in CALENDAR_KINDS
            and filters.account_handle in (None, account.handle)
        ]

        with self._sync:
            now = self._clock()
            fetched, failed = self._read_accounts(
                selected,
                lambda account: open_source(
                    account.kind, account.source, account.credentials, self._imessage_max_silence
                ).fetch(window_start(now), now),
            )
            for account, mailbox in fetched:
                # Each item is classified on its own, outside any database transaction;
                # one that is unchanged since its last classification is not classified
                # again. The account's own inference settings are part of what a stored
                # classification depends on, and decide which backend answers for it.
                config_digest = self._config.digest(account.name)
                classifier = self._classifier_for(account.name)
                prepared = self._store.prepare_mailbox(account.handle, mailbox, now, config_digest)
                classifications = [
                    item.cached or classifier.classify(item.item.text, now)
                    for item in prepared.items
                ]
                self._store.commit_mailbox(
                    account.kind, prepared, classifications, config_digest, now
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

    def _respond_calendar(
        self, name: str, spec: ToolSpec, request: RangeFilters, grant: Grant
    ) -> types.CallToolResult:
        if not isinstance(request, RangeInput | EventsInput):
            return error_result("internal_error", spec.output)
        selector = request.sort if isinstance(request, EventsInput) else ""
        paging = self._paging(name, request.only_filters(), selector, request.cursor, grant)
        if paging is None:
            return error_result("invalid_cursor", spec.output)
        # As for items: an account handle that is not a calendar account of this token selects
        # nothing.
        selected = [
            account
            for account in grant.accounts
            if account.kind in CALENDAR_KINDS and request.account_handle in (None, account.handle)
        ]

        with self._sync:
            now = self._clock()
            fetched, failed = self._read_accounts(
                selected,
                lambda account: open_calendar(
                    account.kind, account.source, account.credentials
                ).fetch_events(now),
            )
            for account, calendar in fetched:
                self._store.commit_calendar(account.handle, account.kind, calendar, now)
            for account, status in failed:
                self._store.mark_unsynced(account.handle, account.kind, status)
            handles = [account.handle for account in selected]
            accounts = self._store.account_sync(handles)
            if failed:
                return error_result("source_unavailable", spec.output, accounts)
            page = self._calendar_page(name, request, handles, now, paging)
            return success_result({"accounts": accounts, **page})

    def _calendar_page(
        self,
        name: str,
        request: RangeInput | EventsInput,
        handles: Sequence[str],
        now: datetime,
        paging: "Paging",
    ) -> dict[str, object]:
        """One page of what a calendar tool answers, as stored, and the cursor of the next."""
        # One row past the page tells whether there is a next one.
        wanted = request.limit + 1
        if isinstance(request, EventsInput):
            key = "events"
            rows = self._store.list_events(
                handles, now, request.only_filters(), request.sort, wanted, paging.offset
            )
        else:
            start, end = look_ahead_range(now, request.range_from, request.range_to)
            blocking = self._store.blocking_events(handles, now, start, end)
            if name == AVAILABILITY_TOOL:
                key = "windows"
                every = windows([(event["start"], event["end"]) for event in blocking], start, end)
            else:
                key = "events"
                every = [dict(event) for event in overlapping(blocking)]
            rows = every[paging.offset : paging.offset + wanted]
        page = rows[: request.limit]
        return {key: page, "next_cursor": paging.next(len(page), len(rows) > len(page))}

    def _paging(
        self, name: str, filters: BaseModel, selector: str, cursor: str | None, grant: Grant
    ) -> "Paging | None":
        """Where the requested page starts: the first, or the one `cursor` stands for. None if
        the cursor is not valid for this caller and query.

        Cursors are bound to the token: another token's cursor, for any account, is as invalid
        as one that was never issued.
        """
        digest = query_digest(name, filters, selector)
        paging = Paging(self._store.cursor_key(), grant.token_id, digest, 0)
        return paging if cursor is None else paging.resume(cursor)

    def _read_accounts[T](
        self, accounts: Sequence[Account], read: Callable[[Account], T]
    ) -> tuple[list[tuple[Account, T]], list[tuple[Account, SyncStatus]]]:
        """Read each account's source, and tell which could not be read and how it stands.

        Every account is tried, so the result tells how each one stands.
        """
        fetched: list[tuple[Account, T]] = []
        failed: list[tuple[Account, SyncStatus]] = []
        for account in accounts:
            try:
                fetched.append((account, read(account)))
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
        return fetched, failed


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
