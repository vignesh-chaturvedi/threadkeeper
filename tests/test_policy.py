"""The stage policy, tested with no database, no graph and no model.

This file is the argument for Phase 03's key decision. Every routing rule in the
product is asserted here in microseconds, because the rules are Python rather
than English embedded in a prompt. The equivalent test for prompt-based gating
would need a model call, would be non-deterministic, and would still only tell
you what happened *once*.
"""

from __future__ import annotations

import pytest

from app.graph.policy import STAGES, Decision, decide, is_terminal


def s(**kw) -> dict:
    base = {"slots": {}, "consent": {}, "interrupt": None, "escalate": False, "stage": "qualify"}
    slots = {**base["slots"], **kw.pop("slots", {})}
    consent = {**base["consent"], **kw.pop("consent", {})}
    return {**base, **kw, "slots": slots, "consent": consent}


# ============================================================ OPT-OUT IS FIRST
class TestOptOut:
    @pytest.mark.parametrize(
        "state",
        [
            s(slots={"opted_out": True}),
            s(interrupt="opt_out"),
            # Even mid-KYC with everything else satisfied.
            s(
                slots={"opted_out": True, "product": "personal_loan", "pan_status": "available"},
                consent={"granted": True},
            ),
            # Even while also asking for a human.
            s(slots={"opted_out": True}, escalate=True),
        ],
    )
    def test_opt_out_wins_from_any_state(self, state: dict) -> None:
        """Revocable consent that waits its turn is not revocable consent."""
        d = decide(state)
        assert d.stage == "close"
        assert d.reason == "customer_opted_out"


# ============================================================== ESCALATION
def test_escalation_beats_funnel_progress() -> None:
    d = decide(s(escalate=True, slots={}))
    assert d.stage == "escalate"
    assert d.reason == "escalation_requested"


def test_escalation_via_interrupt() -> None:
    assert decide(s(interrupt="escalate")).stage == "escalate"


# =============================================================== INTERRUPTS
class TestInterrupts:
    def test_an_objection_pauses_rather_than_advances(self) -> None:
        d = decide(s(interrupt="objection", slots={"product": "personal_loan"}))
        assert d.stage == "handle_objection"
        assert d.holds_stage is True, "answering an objection must not advance the funnel"

    def test_off_topic_pauses_too(self) -> None:
        d = decide(s(interrupt="off_topic", slots={"product": "personal_loan"}))
        assert d.stage == "handle_off_topic"
        assert d.holds_stage is True

    def test_an_objection_does_not_grant_consent(self) -> None:
        """The bug prompt-based gating produces: any reply looks like progress."""
        d = decide(s(interrupt="objection", slots={"product": "personal_loan"}, consent={}))
        assert d.stage == "handle_objection"
        assert d.stage != "kyc_collect"


# ============================================================== FUNNEL GATES
class TestGates:
    def test_no_product_means_qualify(self) -> None:
        d = decide(s(slots={}))
        assert (d.stage, d.reason) == ("qualify", "product_unknown")

    def test_product_but_no_consent_means_consent(self) -> None:
        d = decide(s(slots={"product": "personal_loan"}))
        assert (d.stage, d.reason) == ("consent", "consent_missing")

    def test_kyc_is_unreachable_without_consent(self) -> None:
        """The rule that matters most. Never negotiable, never a prompt's job."""
        state = s(slots={"product": "personal_loan", "income_band": "50k_1l"}, consent={})
        assert decide(state).stage == "consent"

    def test_offers_are_unreachable_without_consent(self) -> None:
        state = s(
            slots={
                "product": "personal_loan",
                "income_band": "50k_1l",
                "pan_status": "available",
            },
            consent={},
        )
        assert decide(state).stage == "consent"

    def test_refused_consent_closes_rather_than_re_asks(self) -> None:
        """Re-prompting someone who said no is how a sales bot becomes a complaint."""
        d = decide(s(slots={"product": "personal_loan"}, consent={"granted": False}))
        assert (d.stage, d.reason) == ("close", "consent_refused")

    def test_consent_granted_but_no_income_returns_to_qualify(self) -> None:
        d = decide(s(slots={"product": "personal_loan"}, consent={"granted": True}))
        assert (d.stage, d.reason) == ("qualify", "income_band_missing")

    def test_income_known_moves_to_kyc(self) -> None:
        d = decide(
            s(
                slots={"product": "personal_loan", "income_band": "50k_1l"},
                consent={"granted": True},
            )
        )
        assert (d.stage, d.reason) == ("kyc_collect", "pan_status_missing")

    def test_pan_absent_stays_at_kyc_with_a_distinct_reason(self) -> None:
        """'Not asked yet' and 'asked, they don't have one' are different states."""
        d = decide(
            s(
                slots={
                    "product": "personal_loan",
                    "income_band": "50k_1l",
                    "pan_status": "missing",
                },
                consent={"granted": True},
            )
        )
        assert (d.stage, d.reason) == ("kyc_collect", "pan_not_available")

    def test_everything_satisfied_reaches_offers(self) -> None:
        d = decide(
            s(
                slots={
                    "product": "personal_loan",
                    "income_band": "50k_1l",
                    "pan_status": "available",
                },
                consent={"granted": True},
            )
        )
        assert (d.stage, d.reason) == ("offer_match", "ready_for_offers")


# ================================================================ PROPERTIES
def test_the_decision_is_pure() -> None:
    """Same input, same output — and the input is never mutated."""
    state = s(slots={"product": "personal_loan"})
    before = repr(state)
    first, second = decide(state), decide(state)
    assert first == second
    assert repr(state) == before, "decide() must not mutate the state it is given"


def test_every_decision_names_a_real_destination() -> None:
    """A typo in a reason string is harmless; a typo in a stage strands a customer."""
    from app.graph.nodes import NODES

    states = [
        s(slots={}),
        s(slots={"product": "personal_loan"}),
        s(slots={"product": "personal_loan"}, consent={"granted": True}),
        s(slots={"product": "x", "income_band": "50k_1l"}, consent={"granted": True}),
        s(
            slots={"product": "x", "income_band": "y", "pan_status": "available"},
            consent={"granted": True},
        ),
        s(slots={"opted_out": True}),
        s(escalate=True),
        s(interrupt="objection"),
        s(interrupt="off_topic"),
        s(consent={"granted": False}, slots={"product": "x"}),
    ]
    for state in states:
        assert decide(state).stage in NODES, f"unreachable destination for {state}"


def test_reasons_are_distinct_enough_to_be_useful() -> None:
    """Every reason lands in stage_transitions; duplicates make the funnel chart lie."""
    reasons = {
        decide(s(slots={})).reason,
        decide(s(slots={"product": "p"})).reason,
        decide(s(slots={"product": "p"}, consent={"granted": True})).reason,
        decide(s(slots={"opted_out": True})).reason,
        decide(s(escalate=True)).reason,
    }
    assert len(reasons) == 5


def test_terminal_stages() -> None:
    assert is_terminal("close") and is_terminal("escalate")
    assert not is_terminal("qualify")


def test_decision_is_frozen() -> None:
    d = Decision("qualify", "why")
    with pytest.raises(AttributeError):
        d.stage = "close"  # type: ignore[misc]


def test_stage_order_is_the_documented_funnel() -> None:
    assert STAGES == (
        "intent_route",
        "qualify",
        "consent",
        "kyc_collect",
        "offer_match",
        "close",
    )
