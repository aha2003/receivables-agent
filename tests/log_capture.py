"""Capture the app's JSON log lines in a test and put the global logging state back afterwards."""

import io
import json
import logging
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from receivables.adapters.log import configure_logging

__all__ = ["JsonLogs", "json_logs"]


class JsonLogs:
    def __init__(self, stream: io.StringIO) -> None:
        self._stream = stream

    def text(self) -> str:
        return self._stream.getvalue()

    def lines(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.text().splitlines() if line.strip()]


@contextmanager
def json_logs(customer_names: Callable[[], Iterable[str]]) -> Iterator[JsonLogs]:
    root = logging.getLogger()
    saved_root = (list(root.handlers), root.level)
    named = ["sqlalchemy", "uvicorn", "uvicorn.error", "uvicorn.access"]
    saved = {n: (logging.getLogger(n).level, list(logging.getLogger(n).handlers),
                 logging.getLogger(n).propagate) for n in named}  # fmt: skip
    stream = io.StringIO()
    root.handlers.clear()  # only ours, as in a server process (pytest's handler re-raises errors)
    configure_logging(customer_names, handler=logging.StreamHandler(stream))
    try:
        yield JsonLogs(stream)
    finally:
        root.handlers[:] = saved_root[0]
        root.setLevel(saved_root[1])
        for name, (level, handlers, propagate) in saved.items():
            logger = logging.getLogger(name)
            logger.setLevel(level)
            logger.handlers[:] = handlers
            logger.propagate = propagate
