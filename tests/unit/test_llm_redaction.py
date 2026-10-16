"""T-SEC-01: no email, phone, TRN, IBAN or customer name reaches the LLM client (NFR-04, D-031).

M-01 (§6.10) with personal data injected goes through ``RedactingClient`` to a capturing fake;
the captured requests are searched for every injected value. All values are synthetic.
"""

import dataclasses
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from llm_capture import CapturingClient

from receivables.adapters.config import Config, load_config
from receivables.adapters.llm.factory import make_llm_client
from receivables.adapters.llm.redacting import RedactingClient, RedactionUnavailable
from receivables.adapters.llm.schemas import LLMCallFailed
from receivables.settings import Settings

EMAIL = "ahmed.k@beta.example"
PHONE = "+971 50 123 4567"
TRN = "100000000100003"
IBAN = "AE070331234567890123456"
CUSTOMER = "Beta Contracting LLC"
NAMES = ("Alpha Trading LLC", CUSTOMER, "Gamma Facilities Services LLC")
INJECTED = (EMAIL, PHONE, TRN, IBAN, CUSTOMER)


def _m01(load_fixture_csv: Callable[[str], list[dict[str, str]]]) -> str:
    row = next(
        r for r in load_fixture_csv("correspondence_labeled.csv") if r["message_id"] == "M-01"
    )
    return (
        f"{row['body']} {CUSTOMER} can be reached at {EMAIL} or {PHONE}. "
        f"Our TRN is {TRN}; pay to IBAN {IBAN}."
    )


def _client(redact: bool = True, names: bool = True) -> tuple[RedactingClient, CapturingClient]:
    capture = CapturingClient()
    client = RedactingClient(
        capture,
        redact_before_send=redact,
        pseudonymize_customer_names=names,
        customer_names=lambda: NAMES,
    )
    return client, capture


def _facts() -> dict[str, Any]:
    # §8.1 fact block, with personal data injected where a careless caller might put it.
    return {
        "customer_name": CUSTOMER,
        "contact_name": f"Accounts Team ({EMAIL})",
        "sender_name": "Collections",
        "invoices": [{"invoice_number": "INV-004", "open_amount": "50000.00",
                      "due_date": "2026-07-01", "days_overdue": 122, "note": f"call {PHONE}"}],
        "as_of": "2026-10-31",
        "tone": 3,
        "dispute_label": "PO_AMOUNT_MISMATCH",
        "amount_difference": "3000.00",
        "bank": {"iban": IBAN, "trn": TRN},
    }  # fmt: skip


@pytest.mark.spec("T-SEC-01")
class TestSec01:
    def test_classify_m01_sends_none_of_the_injected_values(
        self, load_fixture_csv: Callable[[str], list[dict[str, str]]]
    ) -> None:
        client, capture = _client()
        result = client.classify(_m01(load_fixture_csv), "en")
        sent = capture.captured()
        assert result.label == "PO_AMOUNT_MISMATCH"
        assert [value for value in INJECTED if value.lower() in sent.lower()] == []
        for token in ("[EMAIL]", "[PHONE]", "[TRN]", "[IBAN]", "[CUSTOMER]"):
            assert token in sent
        # What the classifier needs survives (§7.2).
        for fact in ("INV-004", "PO-B-004", "47,000", "50,000"):
            assert fact in sent

    def test_plan_and_draft_send_none_of_the_injected_values(self) -> None:
        client, capture = _client()
        client.plan({"invoice_number": "INV-004", "customer": CUSTOMER, "facts": _facts()})
        client.draft(_facts())
        sent = capture.captured()
        assert [value for value in INJECTED if value.lower() in sent.lower()] == []
        assert '"customer_name":"[CUSTOMER]"' in sent  # option a: the drafter sees the placeholder
        assert '"open_amount":"50000.00"' in sent and '"days_overdue":122' in sent

    def test_the_names_are_read_at_every_call(self) -> None:
        names: list[str] = []
        capture = CapturingClient()
        client = RedactingClient(
            capture,
            redact_before_send=True,
            pseudonymize_customer_names=True,
            customer_names=lambda: names,
        )
        client.classify("Delta Supplies LLC will pay.", None)
        names.append("Delta Supplies LLC")  # imported after the client was built
        client.classify("Delta Supplies LLC will pay.", None)
        first, second = capture.captured().split("\n")
        assert "Delta Supplies LLC" in first and "Delta Supplies LLC" not in second

    @pytest.mark.parametrize(
        ("redact", "names", "leaked"),
        [
            (True, True, set()),
            (True, False, {CUSTOMER}),
            (False, True, {EMAIL, PHONE, TRN, IBAN}),
            (False, False, set(INJECTED)),
        ],
    )
    def test_the_switches_decide_what_is_sent(
        self,
        load_fixture_csv: Callable[[str], list[dict[str, str]]],
        redact: bool,
        names: bool,
        leaked: set[str],
    ) -> None:
        client, capture = _client(redact, names)
        client.classify(_m01(load_fixture_csv), "en")
        sent = capture.captured()
        assert {value for value in INJECTED if value in sent} == leaked


def _config(**llm: Any) -> Config:
    base = load_config(Path("/nonexistent/config.yaml"))
    return dataclasses.replace(base, llm=dataclasses.replace(base.llm, **llm))


@pytest.mark.parametrize(("redact", "names"), [(True, True), (False, True), (True, False)])
def test_the_factory_always_wraps_with_the_config_switches(redact: bool, names: bool) -> None:
    config = _config(redact_before_send=redact, pseudonymize_customer_names=names)
    client = make_llm_client(Settings(), config, customer_names=lambda: NAMES)
    assert isinstance(client, RedactingClient)
    assert (client._redact, client._pseudonymize) == (redact, names)  # pyright: ignore[reportPrivateUsage]


def test_non_text_values_pass_through_unchanged() -> None:
    client, capture = _client()
    client.draft({"tone": 3, "paid": False, "note": None, "rows": [1, 2.5], "pair": ("x", 4)})
    assert '"tone":3' in capture.captured() and '"pair":["x",4]' in capture.captured()
    assert '"paid":false' in capture.captured() and '"note":null' in capture.captured()


_DIGITS = st.text(alphabet="0123456789", min_size=15, max_size=15)
_LEAF = st.sampled_from([EMAIL, PHONE, IBAN, CUSTOMER, CUSTOMER.upper()]) | _DIGITS
_PAYLOAD = st.recursive(
    _LEAF | st.integers(-(10**6), 10**6) | st.booleans() | st.none(),  # numbers: code-defined
    lambda inner: (
        st.lists(inner, max_size=3)
        | st.dictionaries(st.sampled_from(["a", "b", "facts", "note"]), inner, max_size=3)
    ),
    max_leaves=12,
)


@given(st.dictionaries(st.sampled_from(["x", "y", "z"]), _PAYLOAD, min_size=1, max_size=3))
def test_planted_values_never_reach_the_client(payload: dict[str, Any]) -> None:
    client, capture = _client()
    client.plan(payload)
    client.draft(payload)
    sent = capture.captured()
    assert [v for v in (EMAIL, PHONE, IBAN) if v in sent] == []
    assert CUSTOMER.lower() not in sent.lower()
    assert not re.search(r"(?<!\d)\d{15}(?!\d)", sent)


def test_unreadable_customer_names_send_nothing_and_fail_as_an_llm_call() -> None:
    def broken() -> tuple[str, ...]:
        raise RuntimeError("database is locked")

    capture = CapturingClient()
    client = RedactingClient(
        capture, redact_before_send=True, pseudonymize_customer_names=True, customer_names=broken
    )
    for call in (
        lambda: client.classify("text", None),
        lambda: client.plan({}),
        lambda: client.draft({}),
    ):
        with pytest.raises(RedactionUnavailable) as error:
            call()
        assert isinstance(error.value, LLMCallFailed)  # callers take the specified fallback
    assert capture.captured() == ""
