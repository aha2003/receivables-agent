"""JSON logs that hold no personal data (SPEC §3.1, NFR-07, T-SEC-04; docs/DECISIONS.md D-031).

The app logs through the standard ``logging`` module; ``configure_logging`` puts one root
handler in place that renders every record (the app's, uvicorn's) as a JSON line through
structlog, after ``Scrubber`` has cleaned it:

- a field whose name says it holds content (a message body, subject, prompt, response, quote,
  address, ...) is replaced with ``[REDACTED]`` whatever it holds;
- every other string value, the message itself included, goes through ``redact`` and
  ``pseudonymize`` (§7.5), whatever the LLM config switches say;
- if the customer names cannot be read, every string is replaced (fail closed).

``RequestIdMiddleware`` gives each HTTP request a fresh id (never taken from the client), adds it
to every log line of that request and returns it as ``X-Request-ID``.
"""

import json
import logging
import sys
import threading
import uuid
from collections.abc import Callable, Iterable, Mapping
from typing import Any

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.typing import EventDict, Processor, WrappedLogger

from receivables.core.redact import pseudonymize, redact

__all__ = [
    "CONTENT_FIELDS",
    "REDACTED",
    "RequestIdMiddleware",
    "Scrubber",
    "configure_logging",
]

REDACTED = "[REDACTED]"
# Field names (compared in lower case) whose values are never logged (NFR-07).
CONTENT_FIELDS = frozenset(
    {
        "body",
        "text",
        "subject",
        "message_body",
        "prompt",
        "system",
        "messages",
        "request",
        "response",
        "raw",
        "content",
        "payload",
        "facts",
        "observation",
        "evidence_quote",
        "quote",
        "summary",
        "summary_en",
        "email",
        "contact_email",
        "phone",
        "trn",
        "iban",
        "customer_name",
        "contact_name",
    }
)
# Added by the pipeline itself; safe as they are (the underscored ones are structlog internals,
# removed before rendering).
_META = frozenset({"level", "logger", "timestamp", "request_id", "_record", "_from_structlog"})
_HANDLER_FLAG = "receivables_json_handler"


class Scrubber:
    """structlog processor: removes content fields and redacts every other string."""

    def __init__(self, customer_names: Callable[[], Iterable[str]]) -> None:
        self._customer_names = customer_names
        self._busy = threading.local()  # reading the names must not log through here again

    def _names(self) -> tuple[str, ...] | None:
        if getattr(self._busy, "on", False):
            return None
        self._busy.on = True
        try:
            return tuple(self._customer_names())
        except Exception:  # no names, no guarantee: fail closed
            return None
        finally:
            self._busy.on = False

    def __call__(self, _: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
        names = self._names()
        return {
            key: value if key in _META else self._field(key, value, names)
            for key, value in event_dict.items()
        }

    def _field(self, key: str, value: Any, names: tuple[str, ...] | None) -> Any:
        if key.lower() in CONTENT_FIELDS and value is not None:
            return REDACTED
        return self._value(value, names)

    def _value(self, value: Any, names: tuple[str, ...] | None) -> Any:
        if value is None or isinstance(value, bool | int | float):
            return value
        if isinstance(value, Mapping):
            return {str(k): self._field(str(k), v, names) for k, v in value.items()}
        if isinstance(value, list | tuple | set | frozenset):
            return [self._value(item, names) for item in value]
        if names is None:
            return REDACTED
        return pseudonymize(redact(str(value)), names)


def _drop_noise(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    for key in ("message", "color_message"):  # duplicates of the event text (logging, uvicorn)
        event_dict.pop(key, None)
    return event_dict


class _SafeFormatter(structlog.stdlib.ProcessorFormatter):
    """Never lets a record out unscrubbed: if formatting fails, a fixed line replaces it.

    (Otherwise ``logging`` would print the raw record and its arguments to stderr.)
    """

    def format(self, record: logging.LogRecord) -> str:
        try:
            return super().format(record)
        except Exception:
            return json.dumps(
                {"event": "log record dropped: formatting failed", "level": "error",
                 "logger": record.name},
                sort_keys=True,
            )  # fmt: skip


class _StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Writes to the current ``sys.stderr`` (which test capture may swap), never a stale one."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def configure_logging(
    customer_names: Callable[[], Iterable[str]],
    *,
    level: int = logging.INFO,
    handler: logging.Handler | None = None,
) -> logging.Handler:
    """Install the JSON, scrubbing root handler (replacing an earlier one) and return it."""
    scrubber = Scrubber(customer_names)
    chain: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.ExtraAdder(),
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
        _drop_noise,
        scrubber,
    ]
    formatter = _SafeFormatter(
        foreign_pre_chain=chain,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(sort_keys=True, ensure_ascii=False),
        ],
    )
    handler = handler if handler is not None else _StderrHandler()
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_FLAG, True)
    root = logging.getLogger()
    for old in [h for h in root.handlers if getattr(h, _HANDLER_FLAG, False)]:
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
    # SQLAlchemy logs SQL with bound values at INFO; uvicorn brings its own handlers.
    logging.getLogger("sqlalchemy").setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers.clear()
        logging.getLogger(name).propagate = True
    return handler


class RequestIdMiddleware:
    """Pure ASGI middleware: a fresh request id in the log context and the response headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid.uuid4().hex
        tokens = structlog.contextvars.bind_contextvars(request_id=request_id)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [(k, v) for k, v in message.get("headers", []) if k != b"x-request-id"]
                message["headers"] = [*headers, (b"x-request-id", request_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            structlog.contextvars.reset_contextvars(**tokens)
