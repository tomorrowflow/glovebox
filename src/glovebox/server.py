"""The gate service: an MCP server over Streamable HTTP, behind operator-issued agent tokens.

It offers the read tools (list accounts, list, count, group and the topic IDs) and nothing
else: no resources, no prompts, no write tool, and no account or token management, which
stays on the operator's command line. A token sees only the tools its operations name. The
server is built on the low-level SDK server so that only the tool-listing and tool-call
handlers exist; capabilities are derived from the handlers, so none can be advertised
by accident.
"""

import os
from collections.abc import Callable, Sequence
from importlib.metadata import version

from mcp import types
from mcp.server import Server, ServerRequestContext
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from glovebox.access import Access
from glovebox.auth import PRINCIPAL_KEY, AgentTokenMiddleware
from glovebox.classification import Classifier, QuarantinedClassifier
from glovebox.classifier_config import ClassifierConfig, load_classifier_config
from glovebox.config import ConfigError, Settings
from glovebox.credentials import CredentialsError, read_credentials
from glovebox.inference import InferenceClassifier
from glovebox.store import Store
from glovebox.stub import StubClassifier
from glovebox.tool import ReadTool

# What the SDK allows by default for a gate on loopback, with every port.
LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOOPBACK_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def build_classifiers(
    settings: Settings, config: ClassifierConfig
) -> Callable[[str], QuarantinedClassifier]:
    """The quarantined classifier to use for an account, by the account's name.

    Each account has exactly one backend: its route in the configuration. There is no
    other classifier to turn to when it fails.
    """
    key_file = config.external.key_file if config.external else None
    if key_file is not None:
        try:
            read_credentials(key_file)
        except CredentialsError as error:
            raise ConfigError(f"inference.external.api_key_file: {error}") from None
    classifiers: dict[str | None, QuarantinedClassifier] = {}

    def make(route: str | None) -> QuarantinedClassifier:
        backend: Classifier | None = None
        if route == "stub":
            backend = StubClassifier.from_files(settings.stub_script, settings.stub_probe)
        elif route == "local":
            backend = InferenceClassifier(config.local, route)
        elif route == "external" and config.external is not None:
            backend = InferenceClassifier(config.external, route)
        return QuarantinedClassifier(backend, config.topics, config.deadline_range)

    def for_account(name: str) -> QuarantinedClassifier:
        route = config.route(name)
        if route not in classifiers:
            classifiers[route] = make(route)
        return classifiers[route]

    return for_account


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
    config = load_classifier_config(settings.classifier_config).resolved(
        settings.standin_inference_local, settings.standin_inference_external
    )
    access = Access(settings.db_path)
    tool = ReadTool(
        Store(settings.db_path),
        access,
        settings.clock(),
        build_classifiers(settings, config),
        config,
        settings.imessage_max_silence,
        settings.mailbox_read_deadline,
    )
    app = create_server(tool).streamable_http_app(
        transport_security=transport_security(settings.allowed_hosts)
    )
    app.add_middleware(AgentTokenMiddleware, access=access)
    return app
