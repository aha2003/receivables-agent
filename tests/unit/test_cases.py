"""Case state machine (SPEC §18.2): T-CASE-01, every table row, guards and properties."""

from datetime import date, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from receivables.agent.cases import (
    Assessed,
    BecameOverdue,
    CaseState,
    CaseTrigger,
    DisputeOpened,
    DisputeResolved,
    MessageSent,
    NoReply,
    PaidInFull,
    PromiseBroken,
    PromiseMade,
    transition,
)

S = CaseState
AS_OF = date(2026, 10, 31)
ALL_STATES: list[CaseState | None] = [None, *CaseState]
NOT_RESOLVED = [s for s in CaseState if s is not S.RESOLVED]


def test_t_case_01_walks_the_full_path() -> None:
    walk: list[CaseTrigger] = [
        BecameOverdue(),
        Assessed(),
        MessageSent(),
        DisputeOpened(),
        DisputeResolved(),
        PromiseMade(promise_date=AS_OF + timedelta(days=7), as_of=AS_OF),
        PromiseBroken(),
        PaidInFull(),
    ]
    state: CaseState | None = None
    seen: list[CaseState | None] = []
    for trigger in walk:
        result = transition(state, trigger)
        assert result.applied, (state, trigger)
        assert result.from_state == state
        state = result.to_state
        seen.append(state)
    assert seen == [
        S.NEW,
        S.ASSESSED,
        S.AWAITING_REPLY,
        S.DISPUTE_OPEN,
        S.ASSESSED,
        S.PROMISE_PENDING,
        S.ESCALATED,
        S.RESOLVED,
    ]


# --- one test per §18.2 row ---


def test_overdue_invoice_without_case_becomes_new() -> None:
    result = transition(None, BecameOverdue())
    assert (result.applied, result.to_state) == (True, S.NEW)


@pytest.mark.parametrize("state", list(CaseState))
def test_becoming_overdue_again_does_not_reopen_an_existing_case(state: CaseState) -> None:
    result = transition(state, BecameOverdue())
    assert (result.applied, result.to_state) == (False, state)


def test_assessment_moves_new_to_assessed_only() -> None:
    assert transition(S.NEW, Assessed()).to_state is S.ASSESSED
    for state in CaseState:
        if state is not S.NEW:
            assert transition(state, Assessed()).applied is False


def test_message_sent_moves_assessed_to_awaiting_reply_only() -> None:
    assert transition(S.ASSESSED, MessageSent()).to_state is S.AWAITING_REPLY
    for state in CaseState:
        if state is not S.ASSESSED:
            result = transition(state, MessageSent())
            assert (result.applied, result.to_state) == (False, state)


@pytest.mark.parametrize("state", NOT_RESOLVED)
def test_dispute_opened_from_any_state_except_resolved(state: CaseState) -> None:
    result = transition(state, DisputeOpened())
    assert (result.applied, result.to_state) == (True, S.DISPUTE_OPEN)


def test_dispute_resolved_returns_to_assessed_only_from_dispute_open() -> None:
    assert transition(S.DISPUTE_OPEN, DisputeResolved()).to_state is S.ASSESSED
    for state in CaseState:
        if state is not S.DISPUTE_OPEN:
            assert transition(state, DisputeResolved()).applied is False


@pytest.mark.parametrize("state", NOT_RESOLVED)
@pytest.mark.parametrize("days_ahead", [0, 1, 30])
def test_promise_on_or_after_as_of_from_any_state_except_resolved(
    state: CaseState, days_ahead: int
) -> None:
    trigger = PromiseMade(promise_date=AS_OF + timedelta(days=days_ahead), as_of=AS_OF)
    result = transition(state, trigger)
    assert (result.applied, result.to_state) == (True, S.PROMISE_PENDING)


@pytest.mark.parametrize("state", NOT_RESOLVED)
def test_promise_dated_before_as_of_is_not_applied(state: CaseState) -> None:
    trigger = PromiseMade(promise_date=AS_OF - timedelta(days=1), as_of=AS_OF)
    result = transition(state, trigger)
    assert (result.applied, result.to_state) == (False, state)


def test_broken_promise_escalates_only_from_promise_pending() -> None:
    result = transition(S.PROMISE_PENDING, PromiseBroken())
    assert (result.applied, result.to_state, result.escalation_task) == (True, S.ESCALATED, True)
    for state in CaseState:
        if state is not S.PROMISE_PENDING:
            assert transition(state, PromiseBroken()).applied is False


@pytest.mark.parametrize(
    ("unanswered", "days_overdue", "escalates"),
    [
        (2, 61, True),
        (3, 120, True),
        (2, 60, False),  # days overdue must be > 60
        (1, 90, False),  # two consecutive unanswered messages needed
        (0, 90, False),
    ],
)
def test_no_reply_escalation_guards(unanswered: int, days_overdue: int, escalates: bool) -> None:
    result = transition(S.AWAITING_REPLY, NoReply(unanswered, days_overdue))
    if escalates:
        assert (result.applied, result.to_state, result.escalation_task) == (
            True,
            S.ESCALATED,
            True,
        )
    else:
        assert (result.applied, result.to_state, result.escalation_task) == (
            False,
            S.AWAITING_REPLY,
            False,
        )


@pytest.mark.parametrize("state", [s for s in CaseState if s is not S.AWAITING_REPLY])
def test_no_reply_escalates_only_from_awaiting_reply(state: CaseState) -> None:
    assert transition(state, NoReply(5, 200)).applied is False


@pytest.mark.parametrize("state", list(CaseState))
def test_paid_in_full_resolves_from_any_state(state: CaseState) -> None:
    result = transition(state, PaidInFull())
    assert (result.applied, result.to_state) == (True, S.RESOLVED)


# --- no case yet, RESOLVED final, escalation task ---

TRIGGERS: list[CaseTrigger] = [
    Assessed(),
    MessageSent(),
    DisputeOpened(),
    DisputeResolved(),
    PromiseMade(promise_date=AS_OF, as_of=AS_OF),
    PromiseBroken(),
    NoReply(3, 90),
    PaidInFull(),
]


@pytest.mark.parametrize("trigger", TRIGGERS)
def test_only_becoming_overdue_creates_a_case(trigger: CaseTrigger) -> None:
    result = transition(None, trigger)
    assert (result.applied, result.to_state) == (False, None)


@pytest.mark.parametrize("trigger", [t for t in TRIGGERS if not isinstance(t, PaidInFull)])
def test_resolved_is_final(trigger: CaseTrigger) -> None:
    result = transition(S.RESOLVED, trigger)
    assert (result.applied, result.to_state) == (False, S.RESOLVED)


def test_staying_in_a_state_is_applied_but_unchanged() -> None:
    again = transition(S.DISPUTE_OPEN, DisputeOpened())
    assert (again.applied, again.to_state) == (True, S.DISPUTE_OPEN)
    promise = PromiseMade(promise_date=AS_OF, as_of=AS_OF)
    assert transition(S.PROMISE_PENDING, promise).to_state is S.PROMISE_PENDING


def test_escalation_task_only_when_entering_escalated() -> None:
    promise = PromiseMade(promise_date=AS_OF, as_of=AS_OF)
    left = transition(S.ESCALATED, promise)
    assert (left.to_state, left.escalation_task) == (S.PROMISE_PENDING, False)
    for state in ALL_STATES:
        for trigger in [BecameOverdue(), *TRIGGERS]:
            result = transition(state, trigger)
            assert result.escalation_task == (
                result.to_state is S.ESCALATED and state is not S.ESCALATED
            )


# --- properties ---

# Every (from, to) pair the §18.2 table allows, including staying put where a row allows it.
ALLOWED = (
    {(None, S.NEW)}
    | {(S.NEW, S.ASSESSED), (S.ASSESSED, S.AWAITING_REPLY), (S.DISPUTE_OPEN, S.ASSESSED)}
    | {(s, S.DISPUTE_OPEN) for s in NOT_RESOLVED}
    | {(s, S.PROMISE_PENDING) for s in NOT_RESOLVED}
    | {(S.PROMISE_PENDING, S.ESCALATED), (S.AWAITING_REPLY, S.ESCALATED)}
    | {(s, S.RESOLVED) for s in CaseState}
)

dates = st.dates(min_value=date(2020, 1, 1), max_value=date(2035, 12, 31))
triggers = st.one_of(
    st.builds(BecameOverdue),
    st.builds(Assessed),
    st.builds(MessageSent),
    st.builds(DisputeOpened),
    st.builds(DisputeResolved),
    st.builds(PromiseMade, promise_date=dates, as_of=dates),
    st.builds(PromiseBroken),
    st.builds(
        NoReply,
        unanswered_outbound=st.integers(0, 10),
        days_overdue=st.integers(-30, 400),
    ),
    st.builds(PaidInFull),
)


@given(st.sampled_from(ALL_STATES), triggers)
def test_every_transition_is_in_the_table_and_deterministic(
    state: CaseState | None, trigger: CaseTrigger
) -> None:
    result = transition(state, trigger)
    assert result == transition(state, trigger)
    assert result.from_state == state and result.trigger == trigger
    if result.applied:
        assert (state, result.to_state) in ALLOWED
    else:
        assert result.to_state == state and not result.escalation_task


@given(st.lists(triggers, max_size=30))
def test_nothing_leaves_resolved(walk: list[CaseTrigger]) -> None:
    state: CaseState | None = S.RESOLVED
    for trigger in walk:
        state = transition(state, trigger).to_state
        assert state is S.RESOLVED
