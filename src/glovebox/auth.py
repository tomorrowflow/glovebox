"""Bearer-token enforcement in front of the whole service.

A pure ASGI middleware, outermost in the app: a request without the configured
token is answered here, so no MCP handler, session or tool ever sees it. One static
token from configuration for now; the operator-issued agent tokens replace it.
"""

import hmac

from starlette.types import ASGIApp, Receive, Scope, Send

REFUSAL_BODY = b'{"error": "unauthorized"}'


class BearerTokenMiddleware:
    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = token.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan" or self._is_authorised(scope):
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
