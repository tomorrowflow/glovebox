"""The gate service: an MCP server over Streamable HTTP.

No tool is registered yet, so the tool list is empty.
"""

from mcp.server import MCPServer
from starlette.applications import Starlette


def create_server() -> MCPServer:
    return MCPServer("glovebox")


def create_app() -> Starlette:
    return create_server().streamable_http_app()
