"""The gate service: an MCP server over Streamable HTTP, behind operator-issued agent tokens.

It offers the read tools (list accounts, list, count, group and the topic IDs) and nothing
else: no resources, no prompts, no write tool, and no account or token management, which
stays on the operator's command line. A token sees only the tools its operations name. The
server is built on the low-level SDK server so that only the tool-listing and tool-call
handlers exist; capabilities are derived from the handlers, so none can be advertised
by accident.
"""

import os
from collections.abc import Sequence
from importlib.metadata import version

from mcp import types
from mcp.server import Server, ServerRequestContext
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from glovebox.access import Access
from glovebox.auth import PRINCIPAL_KEY, AgentTokenMiddleware
from glovebox.classification import Classifier, QuarantinedClassifier
from glovebox.classifier_config import ClassifierConfig, load_classifier_config
from glovebox.config import Settings
from glovebox.store import Store
from glovebox.stub import StubClassifier
from glovebox.tool import ReadTool

# What the SDK allows by default for a gate on loopback, with every port.
LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOOPBACK_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def build_classifier(settings: Settings, config: ClassifierConfig) -> QuarantinedClassifier:
    backend: Classifier | None = None
    if config.backend == "stub":
        backend = StubClassifier.from_files(settings.stub_script, settings.stub_probe)
    return QuarantinedClassifier(backend, config.topics, config.deadline_range)


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


def transport_security(allowed_hosts: Sequence[str]) -> TransportSecuritySettings:
    """DNS-rebinding protection: loopback hosts and origins, plus exactly the hosts listed.

    A Host header outside the list gets 421. Browser origins stay loopback only: an agent
    is not a browser, so nothing listed here may be used from a page.
    """
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[*LOOPBACK_HOSTS, *allowed_hosts],
        allowed_origins=LOOPBACK_ORIGINS,
    )


def create_app(settings: Settings | None = None) -> Starlette:
    settings = settings or Settings.from_env(os.environ)
    config = load_classifier_config(settings.classifier_config)
    access = Access(settings.db_path)
    tool = ReadTool(
        Store(settings.db_path),
        access,
        settings.clock(),
        build_classifier(settings, config),
        config,
        settings.imessage_max_silence,
    )
    app = create_server(tool).streamable_http_app(
        transport_security=transport_security(settings.allowed_hosts)
    )
    app.add_middleware(AgentTokenMiddleware, access=access)
    return app
