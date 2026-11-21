"""Bearer-token enforcement in front of the whole service.

A pure ASGI middleware, outermost in the app: a request without the configured
token is answered here, so no MCP handler, session or tool ever sees it. One static
token from configuration for now; the operator-issued agent tokens replace it.
"""

import hashlib
import hmac

from starlette.types import ASGIApp, Receive, Scope, Send

REFUSAL_BODY = b'{"error": "unauthorized"}'
PRINCIPAL_KEY = "principal"


def principal_of(token: str) -> str:
    """The identity a token stands for, without being the token: a digest of it.

    Page cursors are bound to this. The operator-issued tokens will have their own ids.
    """
    return hashlib.sha256(b"glovebox-principal\0" + token.encode()).hexdigest()


class BearerTokenMiddleware:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = token.encode()
        self._principal = principal_of(token)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
        elif self._is_authorised(scope):
            # The identity is set here and nowhere else, after the token is checked: a
            # request cannot arrive with one of its own.
            scope.setdefault("state", {})[PRINCIPAL_KEY] = self._principal
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

    def _is_authorised(self, scope: Scope) -> bool:
        values = [value for name, value in scope["headers"] if name == b"authorization"]
        if len(values) != 1:
            return False
        scheme, _, presented = values[0].partition(b" ")
        # compare_digest takes the time of the comparison from the presented length only.
        return scheme == b"Bearer" and hmac.compare_digest(presented, self._token)
