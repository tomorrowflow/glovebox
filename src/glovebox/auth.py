"""Agent-token enforcement in front of the whole service.

A pure ASGI middleware, outermost in the app: a request without an active agent token is
answered here, so no MCP handler, session or tool ever sees it. The token is checked
against the database on every request, so a revoked token is refused on its next one.
"""

import anyio.to_thread
from starlette.types import ASGIApp, Receive, Scope, Send

from glovebox.access import TOKEN_PATTERN, Access

REFUSAL_BODY = b'{"error": "unauthorized"}'
PRINCIPAL_KEY = "principal"


class AgentTokenMiddleware:
    def __init__(self, app: ASGIApp, access: Access) -> None:
        self._app = app
        self._access = access

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        principal = await self._principal(scope)
        if principal is not None:
            # The identity is set here and nowhere else, after the token is checked: a
            # request cannot arrive with one of its own. It is the token's id, not its value.
            scope.setdefault("state", {})[PRINCIPAL_KEY] = principal
            await self._app(scope, receive, send)
        elif scope["type"] == "http":
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(REFUSAL_BODY)).encode()),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": REFUSAL_BODY})
        elif scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})

    async def _principal(self, scope: Scope) -> str | None:
        values = [value for name, value in scope["headers"] if name == b"authorization"]
        if len(values) != 1:
            return None
        scheme, _, presented = values[0].partition(b" ")
        try:
            token = presented.decode("ascii")
        except UnicodeDecodeError:
            return None
        if scheme != b"Bearer" or not TOKEN_PATTERN.fullmatch(token):
            return None
        return await anyio.to_thread.run_sync(self._access.authenticate, token)
