"""A real MCP client over Streamable HTTP, as an agent would connect (ADR 0006)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from tests.conftest import RunningGate

ACCOUNTS_TOOL = "list_accounts"
LIST_TOOL = "list_items"
COUNT_TOOL = "count_items"
GROUP_TOOL = "group_items"
TOPICS_TOOL = "list_topics"
AVAILABILITY_TOOL = "get_availability"
EVENTS_TOOL = "list_events"
OVERLAPS_TOOL = "list_overlaps"
# The tools that read items, which need a source; `list_accounts` and `list_topics` do not.
READ_TOOLS = [LIST_TOOL, COUNT_TOOL, GROUP_TOOL]
# The tools that read calendar events.
CALENDAR_TOOLS = [AVAILABILITY_TOOL, EVENTS_TOOL, OVERLAPS_TOOL]
ALL_TOOLS = [ACCOUNTS_TOOL, *READ_TOOLS, TOPICS_TOOL, *CALENDAR_TOOLS]


@asynccontextmanager
async def agent_client(gate: RunningGate, token: str | None = None) -> AsyncIterator[Client]:
    """Connect with the gate's default token, or with the token value `token` instead."""
    bearer = gate.token if token is None else token
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {bearer}"}, timeout=30
    ) as http:
        async with Client(streamable_http_client(gate.url, http_client=http)) as client:
            yield client
