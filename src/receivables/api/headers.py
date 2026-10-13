"""Security headers on every response (SPEC §3.3; T-SEC-11)."""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = ["SECURITY_HEADERS", "SecurityHeadersMiddleware"]

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ]
)
SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}
_RAW = [(k.lower().encode(), v.encode()) for k, v in SECURITY_HEADERS.items()]
_NAMES = {k for k, _ in _RAW}


class SecurityHeadersMiddleware:
    """Pure ASGI middleware: sets the headers on every HTTP response, replacing any existing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() not in _NAMES]
                message["headers"] = headers + _RAW
            await send(message)

        await self.app(scope, receive, send_with_headers)
