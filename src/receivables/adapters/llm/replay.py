"""Recorded LLM responses (SPEC §3.5; T-LLM-04; docs/DECISIONS.md D-029).

``ReplayClient`` serves responses recorded in cassette files under ``llm.cassette_dir``
(``tests/cassettes/``): byte-identical raw text for a recorded request, and ``CassetteMiss``
for anything else. It only reads local files and never opens a network connection. It is also
the disclosed backup for a live demo (T-DEMO-01).

Cassette format (JSON, one file per cassette)::

    {"format": 1, "name": "...", "synthetic": true,
     "interactions": [{"kind": "classify", "request_sha256": "<hex>",
                       "prompt_version": "classify-v1",
                       "model": "<model id or null>", "request": {...}, "response": "<raw text>"}]}

``request_sha256`` is ``request_key(kind, request)``; it is recomputed on load, so an edited
request or a stale prompt version is caught. (It is not named ``key``: secret scanners read a
long hex value next to the word "key" as a credential.) Cassettes hold synthetic, redacted
requests and model output only: never keys, headers or account identifiers.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from receivables.adapters.llm.base import PROMPT_VERSIONS, RawLLMClient, request_key

__all__ = ["CASSETTE_FORMAT", "CassetteError", "CassetteMiss", "ReplayClient", "load_cassettes"]

CASSETTE_FORMAT = 1


class CassetteError(ValueError):
    """A cassette file is missing, malformed or inconsistent."""


class CassetteMiss(LookupError):
    """No recorded response for this request (the request content is never echoed)."""

    def __init__(self, kind: str, key: str) -> None:
        super().__init__(
            f"no recorded {kind} response for request {key[:16]}; record it in a manual "
            "session or script the test with FakeClient"
        )
        self.kind = kind
        self.key = key


class _Interaction(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: Literal["classify", "plan", "draft"]
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_version: str
    model: str | None
    request: dict[str, Any]
    response: str


class _Cassette(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    format: Literal[1]
    name: str
    synthetic: Literal[True]
    interactions: list[_Interaction]


def _read(path: Path) -> _Cassette:
    try:
        return _Cassette.model_validate_json(path.read_bytes())
    except ValidationError:
        raise CassetteError(
            f"{path.name} is not a valid format {CASSETTE_FORMAT} cassette"
        ) from None


def load_cassettes(location: Path) -> dict[str, tuple[str, str]]:
    """Recorded responses by request key, from one cassette file or every ``*.json`` in a folder.

    Returns ``{key: (kind, raw response)}``. Refuses a key whose recomputed hash differs, a
    prompt version other than the current one, and one key recorded with two responses.
    """
    if location.is_dir():
        paths = sorted(location.glob("*.json"))
    elif location.is_file():
        paths = [location]
    else:
        raise CassetteError(f"no cassette at {location}")
    recorded: dict[str, tuple[str, str]] = {}
    for path in paths:
        for item in _read(path).interactions:
            if item.prompt_version != PROMPT_VERSIONS[item.kind]:
                raise CassetteError(f"{path.name}: recorded with an older {item.kind} prompt")
            if request_key(item.kind, item.request) != item.request_sha256:
                raise CassetteError(f"{path.name}: a key does not match its request")
            previous = recorded.get(item.request_sha256)
            if previous is not None and previous != (item.kind, item.response):
                raise CassetteError(f"{path.name}: one request recorded with two responses")
            recorded[item.request_sha256] = (item.kind, item.response)
    return recorded


class ReplayClient(RawLLMClient):
    model = "replay"

    def __init__(self, location: Path) -> None:
        self._recorded = load_cassettes(location)

    def complete(self, kind: str, payload: Mapping[str, Any]) -> str:
        key = request_key(kind, payload)  # the key covers the kind, so kinds cannot mix
        found = self._recorded.get(key)
        if found is None:
            raise CassetteMiss(kind, key)
        return found[1]


def interaction(
    kind: str, request: Mapping[str, Any], response: str, model: str | None = None
) -> dict[str, Any]:
    """One cassette interaction in the stored format (for recording sessions and tests)."""
    return {
        "kind": kind,
        "request_sha256": request_key(kind, request),
        "prompt_version": PROMPT_VERSIONS[kind],
        "model": model,
        "request": json.loads(json.dumps(request)),
        "response": response,
    }
