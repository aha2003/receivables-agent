"""Request body size limit (uploads are at most 5 MB; nothing legitimate is larger)."""

from starlette.types import ASGIApp, Receive, Scope, Send

from receivables.api.auth import error_response

__all__ = ["MAX_BODY_BYTES", "MAX_UPLOAD_BYTES", "BodySizeLimitMiddleware"]

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_BODY_BYTES = MAX_UPLOAD_BYTES + 256 * 1024  # room for multipart framing and form fields


class BodySizeLimitMiddleware:
    """Reject a declared ``Content-Length`` above the limit, and stop streams that exceed it."""

    def __init__(self, app: ASGIApp, limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        declared = headers.get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > self.limit):
            await _too_large(scope, receive, send)
            return
        seen = 0
        exceeded = False
        replaced = False

        async def limited_receive() -> dict[str, object]:
            nonlocal seen, exceeded
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))  # pyright: ignore[reportArgumentType]
                if seen > self.limit:
                    exceeded = True
                    raise _BodyTooLarge
            return message  # pyright: ignore[reportReturnType]

        async def guarded_send(message: dict[str, object]) -> None:
            # Frameworks may turn the overflow into their own error (FastAPI answers 400 for
            # any body-parsing failure); whatever the app says, the client gets the 413.
            nonlocal replaced
            if exceeded:
                if not replaced:
                    replaced = True
                    await _too_large(scope, receive, send)
                return
            await send(message)  # pyright: ignore[reportArgumentType]

        try:
            await self.app(scope, limited_receive, guarded_send)  # pyright: ignore[reportArgumentType]
        except _BodyTooLarge:
            if not replaced:
                await _too_large(scope, receive, send)


class _BodyTooLarge(Exception):
    pass


async def _too_large(scope: Scope, receive: Receive, send: Send) -> None:
    response = error_response(413, "payload_too_large", "Uploads are limited to 5 MB.")
    await response(scope, receive, send)
