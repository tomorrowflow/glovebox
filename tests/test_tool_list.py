"""Acceptance test at the MCP tool-call boundary (ADR 0006).

An MCP client connects to the running service over Streamable HTTP, as an agent
would, and lists tools. The scaffold has no tool yet, so the list is empty.
"""

import pytest
from mcp import Client

from tests.conftest import RunningGate


@pytest.mark.anyio
async def test_client_connects_and_lists_no_tools(gate: RunningGate) -> None:
    async with Client(gate.url) as client:
        result = await client.list_tools()

    assert result.tools == []
