"""The mock lender and the guard — no database, no protocol, no model."""

from __future__ import annotations

import pytest

from app.settings import get_settings
from app.tools import guard, lender


# ===========================================================================
# the mock lender
# ===========================================================================
class TestLender:
    async def test_offers_are_deterministic(self) -> None:
        """The property that makes hallucination falsifiable.

        If the same request produced a different rate each time, "the agent
        invented this number" would be unprovable.
        """
        a = await lender.match("personal_loan", "50k_1l", 1, 500_000)
        b = await lender.match("personal_loan", "50k_1l", 1, 500_000)
        assert a == b
        assert a, "a qualified lead should get offers"

    async def test_offers_are_ranked_cheapest_first(self) -> None:
        offers = await lender.match("personal_loan", "above_1l", 1, 500_000)
        aprs = [o["apr_pct"] for o in offers]
        assert aprs == sorted(aprs)

    async def test_at_most_three_offers(self) -> None:
        offers = await lender.match("personal_loan", "above_1l", 1, 300_000)
        assert len(offers) <= 3

    async def test_eligibility_refusals_say_why(self) -> None:
        """A refusal without a reason is not something an agent can act on."""
        result = await lender.check_eligibility("home_loan", "under_25k", 3, 50_000)
        assert result.eligible is False
        assert result.refusals
        assert all(r["reason"] for r in result.refusals)

    @pytest.mark.parametrize(
        ("product", "income", "tier", "amount", "why"),
        [
            ("home_loan", "under_25k", 1, 5_000_000, "income_below_minimum"),
            ("personal_loan", "above_1l", 3, 1_400_000, "city_not_serviced"),
            ("personal_loan", "above_1l", 1, 50_000_000, "amount_above_maximum"),
        ],
    )
    async def test_rules_actually_refuse(
        self, product: str, income: str, tier: int, amount: int, why: str
    ) -> None:
        result = await lender.check_eligibility(product, income, tier, amount)
        assert why in [r["reason"] for r in result.refusals]

    async def test_a_better_income_band_earns_a_better_rate(self) -> None:
        low = await lender.match("personal_loan", "25k_50k", 1, 500_000)
        high = await lender.match("personal_loan", "above_1l", 1, 500_000)
        assert high[0]["apr_pct"] < low[0]["apr_pct"]

    async def test_emi_is_arithmetically_right(self) -> None:
        """5 lakh at 12% over 36 months is about ₹16,607. Worth checking once."""
        assert lender._emi(500_000, 12.0, 36) == pytest.approx(16_607, abs=5)

    @pytest.mark.parametrize(
        ("pan", "valid"),
        [
            ("ABCDE1234F", True),
            ("abcde1234f", True),  # case is not the customer's problem
            ("ABCD1234F", False),  # too short
            ("ABCDE12345", False),  # last char must be a letter
            ("12345ABCDE", False),
            ("", False),
        ],
    )
    async def test_pan_structure(self, pan: str, valid: bool) -> None:
        result = await lender.verify_pan(pan)
        assert result["verified"] is valid

    async def test_failure_injection_actually_fires(self, monkeypatch) -> None:
        """Set it to 1.0 and every call must fail — otherwise the knob is a lie."""
        monkeypatch.setattr(get_settings(), "lender_failure_rate", 1.0)
        with pytest.raises((lender.LenderTimeout, lender.LenderUnavailable)):
            await lender.match("personal_loan", "above_1l", 1, 500_000)

    async def test_no_failures_when_the_rate_is_zero(self, monkeypatch) -> None:
        monkeypatch.setattr(get_settings(), "lender_failure_rate", 0.0)
        for _ in range(20):
            assert await lender.match("personal_loan", "above_1l", 1, 500_000)


# ===========================================================================
# the guard — the phase's key decision
# ===========================================================================
def _state(**kw):
    base = {"slots": {}, "consent": {}}
    return {**base, **kw}


class TestGuard:
    def test_create_application_is_impossible_without_consent(self) -> None:
        """The plan's exact requirement, at the stage where it is allowed."""
        verdict = guard.check("create_application", "close", _state())
        assert not verdict
        assert verdict.reason == "consent_missing"

    def test_create_application_is_impossible_at_the_wrong_stage(self) -> None:
        """Even with consent — two independent locks on the same door."""
        state = _state(consent={"granted": True}, slots={"pan_status": "available"})
        assert not guard.check("create_application", "qualify", state)
        assert guard.check("create_application", "close", state)

    def test_create_application_needs_kyc_too(self) -> None:
        verdict = guard.check("create_application", "close", _state(consent={"granted": True}))
        assert not verdict
        assert verdict.reason == "kyc_incomplete"

    def test_fetching_offers_requires_consent(self) -> None:
        """Fetching offers means sending details to lenders — the thing consent covers."""
        assert not guard.check("fetch_offers", "offer_match", _state())
        assert guard.check("fetch_offers", "offer_match", _state(consent={"granted": True}))

    def test_no_tools_at_all_during_consent(self) -> None:
        """The customer is being asked a question. Calling a lender mid-question is overreach."""
        for tool in ("fetch_offers", "check_eligibility", "verify_pan", "create_application"):
            assert not guard.check(tool, "consent", _state(consent={"granted": True}))

    def test_escalating_works_from_every_stage(self) -> None:
        """Same reasoning that puts opt-out first in the stage policy."""
        for stage in guard.ALLOWED:
            assert guard.check("escalate_to_human", stage, _state()), stage

    def test_an_opt_out_blocks_every_write(self) -> None:
        state = _state(
            slots={"opted_out": True, "pan_status": "available"}, consent={"granted": True}
        )
        assert not guard.check("create_application", "close", state)
        assert not guard.check("schedule_followup", "close", state)

    def test_an_unknown_stage_permits_nothing_extra(self) -> None:
        """Fail closed. A new stage should not silently unlock the lender."""
        assert not guard.check("fetch_offers", "some_new_stage", _state(consent={"granted": True}))
        assert guard.check("escalate_to_human", "some_new_stage", _state())

    def test_verify_pan_only_at_kyc(self) -> None:
        assert guard.check("verify_pan", "kyc_collect", _state())
        assert not guard.check("verify_pan", "qualify", _state())

    def test_write_tools_are_declared(self) -> None:
        """Idempotency keys are applied by membership of this set, so it matters."""
        assert guard.WRITE_TOOLS == frozenset({"create_application", "schedule_followup"})

    def test_tools_for_stage_never_omits_the_escape_hatches(self) -> None:
        for stage in (*guard.ALLOWED, "nonsense"):
            assert guard.ALWAYS_ALLOWED <= guard.tools_for_stage(stage)
