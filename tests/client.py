"""A real MCP client over Streamable HTTP, as an agent would connect (ADR 0006)."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from tests.conftest import RunningGate

LIST_TOOL = "list_items"
COUNT_TOOL = "count_items"
GROUP_TOOL = "group_items"
READ_TOOLS = [LIST_TOOL, COUNT_TOOL, GROUP_TOOL]


@asynccontextmanager
async def agent_client(gate: RunningGate, token: str | None = None) -> AsyncIterator[Client]:
    """Connect with the gate's token, or with `token` instead."""
    bearer = gate.token if token is None else token
    async with httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {bearer}"}, timeout=30
    ) as http:
        async with Client(streamable_http_client(gate.url, http_client=http)) as client:
            yield client
