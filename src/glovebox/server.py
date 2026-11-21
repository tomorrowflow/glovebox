"""The gate service: an MCP server over Streamable HTTP, behind a bearer token.

It offers four read tools (list, count, group and the topic IDs) and nothing else: no resources, no
prompts, no write tool. The server is built on the low-level SDK server so that only
the tool-listing and tool-call handlers exist; capabilities are derived from the
handlers, so none can be advertised by accident.
"""

import os
from importlib.metadata import version

from mcp import types
from mcp.server import Server, ServerRequestContext
from starlette.applications import Starlette

from glovebox.auth import PRINCIPAL_KEY, BearerTokenMiddleware
from glovebox.classification import Classifier, QuarantinedClassifier
from glovebox.classifier_config import ClassifierConfig, load_classifier_config
from glovebox.config import Settings
from glovebox.sources import FixtureMailSource, MailSource
from glovebox.store import Store
from glovebox.stub import StubClassifier
from glovebox.tool import TOOLS, ReadTool


def build_source(settings: Settings) -> MailSource | None:
    if settings.mail_url is not None:
        return FixtureMailSource(settings.mail_url)
    if settings.mail_fixture is not None:
        return FixtureMailSource(settings.mail_fixture)
    return None


def build_classifier(settings: Settings, config: ClassifierConfig) -> QuarantinedClassifier:
    backend: Classifier | None = None
    if config.backend == "stub":
        backend = StubClassifier.from_files(settings.stub_script, settings.stub_probe)
    return QuarantinedClassifier(backend, config.topics, config.deadline_range)


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
    config = load_classifier_config(settings.classifier_config)
    tool = ReadTool(
        Store(settings.db_path),
        build_source(settings),
        settings.clock(),
        build_classifier(settings, config),
        config,
    )
    app = create_server(tool).streamable_http_app()
    app.add_middleware(BearerTokenMiddleware, token=settings.agent_token)
    return app
