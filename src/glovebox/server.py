"""The gate service: an MCP server over Streamable HTTP, behind a bearer token.

It offers three read tools (list, count, group) and nothing else: no resources, no
prompts, no write tool. The server is built on the low-level SDK server so that only
the two tool handlers exist; capabilities are derived from the handlers, so none can be
advertised by accident.
"""

import os
from importlib.metadata import version

from mcp import types
from mcp.server import Server, ServerRequestContext
from starlette.applications import Starlette

from glovebox.auth import PRINCIPAL_KEY, BearerTokenMiddleware
from glovebox.config import Settings
from glovebox.sources import FixtureMailSource, MailSource
from glovebox.store import Store
from glovebox.tool import TOOLS, ReadTool


def build_source(settings: Settings) -> MailSource | None:
    if settings.mail_url is not None:
        return FixtureMailSource(settings.mail_url)
    if settings.mail_fixture is not None:
        return FixtureMailSource(settings.mail_fixture)
    return None


def principal_of_request(ctx: ServerRequestContext) -> str | None:
    """The authenticated caller of this request, as the auth middleware recorded it."""
    state = getattr(ctx.request, "state", None)
    principal = getattr(state, PRINCIPAL_KEY, None)
    return principal if isinstance(principal, str) else None


def create_server(tool: ReadTool) -> Server:
    async def list_tools(
        ctx: ServerRequestContext, params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        return types.ListToolsResult(tools=TOOLS)

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
    tool = ReadTool(Store(settings.db_path), build_source(settings), settings.clock())
    app = create_server(tool).streamable_http_app()
    app.add_middleware(BearerTokenMiddleware, token=settings.agent_token)
    return app
