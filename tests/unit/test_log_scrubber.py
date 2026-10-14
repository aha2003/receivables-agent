"""The scrubbing log pipeline (SPEC §3.1, NFR-07; D-031). All values are synthetic."""

import logging
from typing import Any

import pytest
import structlog
from log_capture import json_logs
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from receivables.adapters.log import REDACTED, RequestIdMiddleware, Scrubber, configure_logging

EMAIL = "ahmed.k@beta.example"
PHONE = "050 123 4567"
TRN = "100000000100003"
IBAN = "AE070331234567890123456"
NAME = "Beta Contracting LLC"
NAMES = (NAME,)
PII = (EMAIL, PHONE, TRN, IBAN, NAME)
log = logging.getLogger("receivables.test")


def scrub(event: dict[str, Any], names: Any = lambda: NAMES) -> dict[str, Any]:
    return dict(Scrubber(names)(None, "info", event))


def test_content_fields_are_removed_whatever_they_hold() -> None:
    event = {"event": "x", "body": "hello", "Subject": "s", "evidence_quote": "q", "text": None}
    assert scrub(event) == {"event": "x", "body": REDACTED, "Subject": REDACTED,
                            "evidence_quote": REDACTED, "text": None}  # fmt: skip


def test_every_other_string_is_redacted_and_pseudonymised() -> None:
    event = {
        "event": f"mail from {EMAIL} for {NAME.upper()}",
        "detail": {"iban": IBAN, "note": f"TRN {TRN}", "list": [PHONE, 3, None, True]},
        "count": 3,
        "level": "info",
        "request_id": "abc",
    }
    out = scrub(event)
    assert out["event"] == "mail from [EMAIL] for [CUSTOMER]"
    assert out["detail"] == {
        "iban": REDACTED,
        "note": "TRN [TRN]",
        "list": ["[PHONE]", 3, None, True],
    }
    assert (out["count"], out["level"], out["request_id"]) == (3, "info", "abc")


def test_without_customer_names_every_string_is_dropped() -> None:
    def broken() -> tuple[str, ...]:
        raise RuntimeError("no customers table yet")

    out = scrub({"event": f"hello {NAME}", "count": 2, "level": "info"}, broken)
    assert out == {"event": REDACTED, "count": 2, "level": "info"}


def test_reading_the_names_cannot_recurse() -> None:
    inner: list[dict[str, Any]] = []

    def names() -> tuple[str, ...]:
        # Reading the names logs (say, a database warning): that record is scrubbed without
        # reading the names again, so it fails closed instead of recursing.
        inner.append(dict(scrubber(None, "info", {"event": f"while reading {NAME}"})))
        return NAMES

    scrubber = Scrubber(names)
    out = scrubber(None, "info", {"event": f"for {NAME}"})
    assert inner == [{"event": REDACTED}]
    assert out["event"] == "for [CUSTOMER]"


def test_records_become_scrubbed_json_lines() -> None:
    with json_logs(lambda: NAMES) as logs:
        log.info("customer %s wrote from %s", NAME, EMAIL, extra={"body": "secret body", "n": 1})
        try:
            raise ValueError(f"bad IBAN {IBAN} for {NAME}")
        except ValueError:
            log.exception("failed")
    first, second = logs.lines()
    assert first["event"] == "customer [CUSTOMER] wrote from [EMAIL]"
    assert (first["body"], first["n"], first["level"], first["logger"]) == (
        REDACTED,
        1,
        "info",
        "receivables.test",
    )
    assert "timestamp" in first
    assert "ValueError" in second["exception"] and "[IBAN]" in second["exception"]
    assert [v for v in PII if v in logs.text()] == []


def test_configuring_twice_keeps_one_handler_and_silences_sql() -> None:
    with json_logs(lambda: NAMES):
        before = len(logging.getLogger().handlers)
        handler = configure_logging(lambda: NAMES, handler=logging.StreamHandler())
        try:
            assert len(logging.getLogger().handlers) == before
            assert logging.getLogger("sqlalchemy").getEffectiveLevel() == logging.WARNING
            assert not logging.getLogger("sqlalchemy.engine.Engine").isEnabledFor(logging.INFO)
        finally:
            logging.getLogger().removeHandler(handler)


def test_uvicorn_records_go_through_the_same_pipeline() -> None:
    logging.getLogger("uvicorn.access").addHandler(logging.NullHandler())
    with json_logs(lambda: NAMES) as logs:
        assert logging.getLogger("uvicorn.access").handlers == []
        logging.getLogger("uvicorn.access").info('%s "%s %s"', "127.0.0.1", "GET", f"/x?m={EMAIL}")
    (line,) = logs.lines()
    assert line["logger"] == "uvicorn.access" and "[EMAIL]" in line["event"]


def _app() -> Starlette:
    async def page(_: Request) -> PlainTextResponse:
        log.info("page served")
        return PlainTextResponse("ok")

    app = Starlette(routes=[Route("/", page)])
    app.add_middleware(RequestIdMiddleware)
    return app


def test_each_request_gets_a_fresh_id_in_its_logs_and_headers() -> None:
    with json_logs(lambda: NAMES) as logs, TestClient(_app()) as client:
        first = client.get("/", headers={"X-Request-ID": "forged\nid"})
        second = client.get("/")
    ids = [first.headers["x-request-id"], second.headers["x-request-id"]]
    assert all(len(i) == 32 and int(i, 16) >= 0 for i in ids) and ids[0] != ids[1]
    served = [line for line in logs.lines() if line["logger"] == "receivables.test"]
    assert [line["request_id"] for line in served] == ids  # never the client's value
    assert structlog.contextvars.get_contextvars() == {}  # nothing leaks to later code


@pytest.mark.parametrize("scope_type", ["lifespan", "websocket"])
def test_non_http_scopes_pass_through(scope_type: str) -> None:
    seen: list[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])

    import asyncio

    asyncio.run(RequestIdMiddleware(inner)({"type": scope_type}, None, None))  # type: ignore[arg-type]
    assert seen == [scope_type]


def test_a_record_that_cannot_be_formatted_is_dropped_not_leaked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with json_logs(lambda: NAMES) as logs:
        log.info("amount %d for %s", f"{NAME} {IBAN}", EMAIL)  # bad format: %d gets a string
    (line,) = logs.lines()
    assert line == {
        "event": "log record dropped: formatting failed",
        "level": "error",
        "logger": "receivables.test",
    }
    printed = capsys.readouterr()
    assert [v for v in PII if v in logs.text() + printed.out + printed.err] == []
