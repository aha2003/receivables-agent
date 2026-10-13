"""Request body size limit: declared and streamed bodies above the limit are refused with 413."""

from collections.abc import Iterator

import anyio
import pytest
from fastapi.testclient import TestClient

from receivables.api.limits import MAX_BODY_BYTES, BodySizeLimitMiddleware

from .conftest import csrf, login


def test_declared_length_above_the_limit_is_413(client: TestClient) -> None:
    assert login(client) == 303
    response = client.post(
        "/api/imports/customers",
        content=b"x",
        headers={"content-length": str(MAX_BODY_BYTES + 1), "x-csrf-token": csrf(client)},
    )
    assert response.status_code == 413
    assert response.json()["error"] == "payload_too_large"


def test_malformed_content_length_is_refused(client: TestClient) -> None:
    assert login(client) == 303
    response = client.post("/logout", content=b"", headers={"content-length": "abc"})
    assert response.status_code == 413


def test_streamed_body_above_the_limit_is_413(client: TestClient) -> None:
    assert login(client) == 303
    chunk = b"y" * (1024 * 1024)

    boundary = "synthetic-boundary"

    def body() -> Iterator[bytes]:
        # A multipart upload streamed with no Content-Length (chunked), 7 MB of file data:
        # the route must read it, so the streaming limit is what stops it.
        yield (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            'filename="big.csv"\r\nContent-Type: text/csv\r\n\r\n'
        ).encode()
        for _ in range(7):
            yield chunk
        yield f"\r\n--{boundary}--\r\n".encode()

    response = client.post(
        "/api/imports/customers",
        content=body(),
        headers={
            "x-csrf-token": csrf(client),
            "content-type": f"multipart/form-data; boundary={boundary}",
        },
    )
    assert response.status_code == 413
    assert response.json()["error"] == "payload_too_large"


def test_non_http_scopes_pass_through() -> None:
    seen: list[str] = []

    async def inner(scope: dict[str, object], receive: object, send: object) -> None:
        seen.append(str(scope["type"]))

    middleware = BodySizeLimitMiddleware(inner)  # pyright: ignore[reportArgumentType]

    async def run() -> None:
        await middleware({"type": "lifespan"}, None, None)  # pyright: ignore[reportArgumentType]

    anyio.run(run)
    assert seen == ["lifespan"]


@pytest.mark.parametrize("size", [0, 1024])
def test_small_bodies_pass(client: TestClient, size: int) -> None:
    assert login(client) == 303
    response = client.post("/logout", data={"csrf_token": csrf(client), "pad": "z" * size})
    assert response.status_code == 303


def test_overflow_raised_through_the_app_still_gives_413() -> None:
    sent: list[dict[str, object]] = []

    async def reader(scope: object, receive: object, send: object) -> None:
        while True:  # reads the body and never catches the overflow
            message = await receive()  # pyright: ignore[reportCallIssue]
            if not message.get("more_body"):  # pyright: ignore[reportAttributeAccessIssue]
                return

    middleware = BodySizeLimitMiddleware(reader, limit=10)  # pyright: ignore[reportArgumentType]
    chunks = iter([b"0123456789", b"overflow!", b""])

    async def receive() -> dict[str, object]:
        body = next(chunks)
        return {"type": "http.request", "body": body, "more_body": bool(body)}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    async def run() -> None:
        scope = {"type": "http", "headers": [], "method": "POST", "path": "/x"}
        await middleware(scope, receive, send)  # pyright: ignore[reportArgumentType]

    anyio.run(run)
    assert sent[0]["type"] == "http.response.start" and sent[0]["status"] == 413
