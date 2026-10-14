"""FakeClient and ReplayClient (SPEC §3.5): T-LLM-04 and the cassette format.

Synthetic data only. No test here, or anywhere in the suite, calls the live Anthropic API.
"""

import json
import socket
from pathlib import Path
from typing import Any

import pytest

from receivables.adapters.llm import base
from receivables.adapters.llm.base import PROMPT_VERSIONS, canonical_request, request_key
from receivables.adapters.llm.fake import FakeClient, FakeScriptExhausted
from receivables.adapters.llm.replay import (
    CassetteError,
    CassetteMiss,
    ReplayClient,
    interaction,
    load_cassettes,
)
from receivables.adapters.llm.schemas import LLMResponseInvalid

ROOT = Path(__file__).resolve().parents[2]
CASSETTES = ROOT / "tests" / "cassettes"
SYNTHETIC = CASSETTES / "s3_01_synthetic.json"


def recorded() -> dict[str, Any]:
    return json.loads(SYNTHETIC.read_text(encoding="utf-8"))


def requests_of(kind: str) -> dict[str, Any]:
    return next(i["request"] for i in recorded()["interactions"] if i["kind"] == kind)


def response_of(kind: str) -> str:
    return next(i["response"] for i in recorded()["interactions"] if i["kind"] == kind)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    attempts: list[str] = []

    def refuse(*args: Any, **kwargs: Any) -> Any:
        attempts.append("network")
        raise OSError("network blocked in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)
    return attempts


# --- T-LLM-04 ----------------------------------------------------------------------------


class TestLlm04:
    def test_the_same_cassette_gives_byte_identical_results(self, no_network: list[str]) -> None:
        first, second = ReplayClient(CASSETTES), ReplayClient(SYNTHETIC)
        for kind in ("classify", "plan", "draft"):
            raw_1 = first.complete(kind, requests_of(kind))
            raw_2 = second.complete(kind, requests_of(kind))
            assert (
                raw_1.encode("utf-8") == raw_2.encode("utf-8") == response_of(kind).encode("utf-8")
            )
        request = requests_of("classify")
        assert first.classify(request["text"], request["language_hint"]) == second.classify(
            request["text"], request["language_hint"]
        )
        assert no_network == []

    def test_an_unrecorded_request_raises_a_clear_error(self, no_network: list[str]) -> None:
        client = ReplayClient(CASSETTES)
        with pytest.raises(CassetteMiss) as miss:
            client.classify("A synthetic message nobody recorded about INV-999.", "en")
        message = str(miss.value)
        assert "no recorded classify response" in message and "FakeClient" in message
        assert "INV-999" not in message  # the request content is never echoed
        assert miss.value.key == request_key(
            "classify",
            {"text": "A synthetic message nobody recorded about INV-999.", "language_hint": "en"},
        )
        assert no_network == []

    def test_no_network_call_is_made(self, no_network: list[str]) -> None:
        client = ReplayClient(CASSETTES)
        plan = client.plan(requests_of("plan")["observation"])
        draft = client.draft(requests_of("draft")["facts"])
        fake = FakeClient(draft=[response_of("draft")])
        assert fake.draft({}) == draft
        assert plan.invoice_number == "INV-003"
        assert no_network == []


def test_replayed_responses_are_validated_models() -> None:
    client = ReplayClient(CASSETTES)
    request = requests_of("classify")
    result = client.classify(request["text"], request["language_hint"])
    assert (result.label, result.invoice_numbers) == ("PO_AMOUNT_MISMATCH", ["INV-004"])
    assert [a.value for a in result.amounts_mentioned] == ["47000", "50000"]
    assert [s.tool for s in client.plan(requests_of("plan")["observation"]).steps] == [
        "draft_message",
        "send_email",
        "schedule_followup",
    ]
    assert (
        client.draft(requests_of("draft")["facts"]).subject == "Payment reminder: invoice INV-003"
    )


# --- cassette format -----------------------------------------------------------------------


def write(tmp_path: Path, cassette: dict[str, Any], name: str = "c.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(cassette), encoding="utf-8")
    return path


def cassette(*interactions: dict[str, Any], **changes: Any) -> dict[str, Any]:
    return {
        "format": 1,
        "name": "t",
        "synthetic": True,
        "interactions": list(interactions),
    } | changes


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"format": 2}, "not a valid"),
        ({"synthetic": False}, "not a valid"),
        ({"extra": 1}, "not a valid"),
        ({"interactions": "x"}, "not a valid"),
    ],
)
def test_malformed_cassettes_are_refused(
    tmp_path: Path, changes: dict[str, Any], message: str
) -> None:
    with pytest.raises(CassetteError, match=message):
        load_cassettes(write(tmp_path, cassette(**changes)))


def test_a_tampered_request_is_refused(tmp_path: Path) -> None:
    item = interaction("draft", {"facts": {"x": 1}}, '{"subject": "s", "body": "b"}')
    item["request"] = {"facts": {"x": 2}}  # request_sha256 no longer matches
    with pytest.raises(CassetteError, match="does not match"):
        load_cassettes(write(tmp_path, cassette(item)))


def test_a_cassette_from_an_older_prompt_is_refused(tmp_path: Path) -> None:
    item = interaction("draft", {"facts": {}}, '{"subject": "s", "body": "b"}')
    item["prompt_version"] = "draft-v0"
    with pytest.raises(CassetteError, match="older draft prompt"):
        load_cassettes(write(tmp_path, cassette(item)))


def test_one_request_with_two_responses_is_refused(tmp_path: Path) -> None:
    a = interaction("draft", {"facts": {}}, '{"subject": "a", "body": "b"}')
    b = interaction("draft", {"facts": {}}, '{"subject": "c", "body": "d"}')
    write(tmp_path, cassette(a), "a.json")
    write(tmp_path, cassette(b), "b.json")
    with pytest.raises(CassetteError, match="two responses"):
        load_cassettes(tmp_path)
    same = tmp_path / "same"
    same.mkdir()
    write(same, cassette(a), "a.json")
    write(same, cassette(a), "b.json")
    assert len(load_cassettes(same)) == 1  # the same response twice is fine


def test_a_missing_location_is_refused(tmp_path: Path) -> None:
    with pytest.raises(CassetteError, match="no cassette"):
        ReplayClient(tmp_path / "nowhere")


def test_invalid_recorded_output_still_fails_validation(tmp_path: Path) -> None:
    item = interaction("draft", {"facts": {}}, "this is not JSON")
    client = ReplayClient(write(tmp_path, cassette(item)))
    with pytest.raises(LLMResponseInvalid, match="not JSON"):
        client.draft({})


def test_cassettes_hold_no_credentials() -> None:
    forbidden = ("sk-ant", "x-api-key", "authorization", "api_key", "bearer ", "anthropic-version")
    for path in CASSETTES.glob("*.json"):
        text = path.read_text(encoding="utf-8").lower()
        assert not [f for f in forbidden if f in text], path.name
        assert json.loads(text)["synthetic"] is True


def test_the_request_key_covers_the_prompt_version(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"facts": {"a": 1}}
    before = request_key("draft", payload)
    assert canonical_request("draft", {"facts": {"a": 1}}) == canonical_request("draft", payload)
    monkeypatch.setitem(base.PROMPT_VERSIONS, "draft", "draft-v2")
    assert request_key("draft", payload) != before
    assert PROMPT_VERSIONS is base.PROMPT_VERSIONS


# --- FakeClient ------------------------------------------------------------------------------


def test_fake_scripts_are_served_in_order_and_recorded() -> None:
    fake = FakeClient(
        draft=[{"subject": "one", "body": "b"}, '{"subject": "two", "body": "b"}'],
        plan=lambda request: {"invoice_number": request["observation"]["n"], "steps": []},
    )
    assert [fake.draft({}).subject, fake.draft({"x": 1}).subject] == ["one", "two"]
    assert fake.plan({"n": "INV-003"}).invoice_number == "INV-003"
    assert [kind for kind, _ in fake.calls] == ["draft", "draft", "plan"]
    assert fake.calls[1] == ("draft", {"facts": {"x": 1}})


def test_fake_runs_out_clearly() -> None:
    fake = FakeClient(classify=[])
    with pytest.raises(FakeScriptExhausted, match="no scripted classify"):
        fake.classify("text", None)
    with pytest.raises(FakeScriptExhausted, match="no scripted draft"):
        fake.draft({})


def test_fake_output_is_validated_like_any_other() -> None:
    fake = FakeClient(classify=["{not json", {"label": "PAID"}])
    with pytest.raises(LLMResponseInvalid, match="not JSON"):
        fake.classify("text", None)
    with pytest.raises(LLMResponseInvalid, match="schema mismatch"):
        fake.classify("text", None)


def test_fake_refuses_unknown_kinds() -> None:
    with pytest.raises(ValueError, match="unknown call kinds"):
        FakeClient(summarise=[])
