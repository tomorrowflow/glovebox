"""The gate service: an MCP server over Streamable HTTP, behind operator-issued agent tokens.

It offers the read tools (list accounts, list, count, group) and nothing else: no
resources, no prompts, no write tool, and no account or token management, which stays
on the operator's command line. A token sees only the tools its operations name. The
server is built on the low-level SDK server so that only the tool-listing and tool-call
handlers exist; capabilities are derived from the handlers, so none can be advertised
by accident.
"""

import os
from importlib.metadata import version

from mcp import types
from mcp.server import Server, ServerRequestContext
from starlette.applications import Starlette

from glovebox.access import Access
from glovebox.auth import PRINCIPAL_KEY, AgentTokenMiddleware
from glovebox.config import Settings
from glovebox.store import Store
from glovebox.tool import ReadTool


def principal_of_request(ctx: ServerRequestContext) -> str | None:
    """The authenticated token of this request, as the auth middleware recorded it."""
    state = getattr(ctx.request, "state", None)
    principal = getattr(state, PRINCIPAL_KEY, None)
    return principal if isinstance(principal, str) else None


def create_server(tool: ReadTool) -> Server:
    async def list_tools(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=await tool.definitions(principal_of_request(ctx)))

    async def call_tool(
        ctx: ServerRequestContext, params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        return await tool.call(params.name, params.arguments, principal_of_request(ctx))

    return Server(
        "glovebox",
        version=version("glovebox"),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or Settings.from_env(os.environ)
    access = Access(settings.db_path)
    tool = ReadTool(Store(settings.db_path), access, settings.clock())
    app = create_server(tool).streamable_http_app()
    app.add_middleware(AgentTokenMiddleware, access=access)
    return app
