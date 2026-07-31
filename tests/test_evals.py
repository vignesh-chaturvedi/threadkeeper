"""The scorecard's own tests.

A scoring function nobody checks is a number nobody should trust. The two hard
failures especially: `hallucinated_rate` decides whether a PR merges, and a
false positive there makes the gate something people learn to ignore.
"""

from __future__ import annotations

import pytest

from evals import personas as persona_lib
from evals import scorecard


# ============================================================== PERSONAS
class TestPersonas:
    def test_all_five_load(self) -> None:
        names = {p.name for p in persona_lib.load_all()}
        assert names == {
            "confused_first_timer",
            "rate_shopper",
            "hinglish_switcher",
            "ghoster",
            "adversarial",
        }

    def test_each_carries_both_a_prompt_and_a_script(self) -> None:
        """The prompt makes the numbers mean something; the script makes CI free."""
        for persona in persona_lib.load_all():
            assert persona.system, f"{persona.name} has no system prompt"
            assert persona.script, f"{persona.name} has no script"
            assert persona.goal, f"{persona.name} has no stated goal"

    def test_each_declares_what_it_expects(self) -> None:
        """A persona without expectations cannot fail, which makes it decoration."""
        for persona in persona_lib.load_all():
            assert persona.expects, f"{persona.name} declares no expectations"

    def test_the_adversarial_persona_forbids_the_hard_failures(self) -> None:
        adversarial = persona_lib.load("adversarial")
        assert set(adversarial.must_not) == {"hallucinated_rate", "off_policy_promise"}

    async def test_the_scripted_persona_is_deterministic(self, monkeypatch) -> None:
        """Fixed seed, comparable runs — the plan is explicit about this."""
        from app.settings import get_settings

        monkeypatch.setattr(get_settings(), "llm_provider", "fake")
        persona = persona_lib.load("confused_first_timer")

        first = await persona_lib.next_message(persona, [])
        second = await persona_lib.next_message(persona, [])
        assert first == second == persona.script[0]

    async def test_the_ghoster_runs_out_of_things_to_say(self, monkeypatch) -> None:
        """Going quiet is the behaviour under test, not an error."""
        from app.settings import get_settings

        monkeypatch.setattr(get_settings(), "llm_provider", "fake")
        persona = persona_lib.load("ghoster")

        transcript: list[tuple[str, str]] = []
        for _ in range(len(persona.script)):
            message = await persona_lib.next_message(persona, transcript)
            assert message is not None
            transcript += [("customer", message), ("agent", "ok")]

        assert await persona_lib.next_message(persona, transcript) is None


# =========================================================== HARD FAILURE 1
class TestHallucinatedNumbers:
    def test_it_ignores_trivial_quantities(self) -> None:
        """ "2 options", "36 months" — flagging these would flag every reply."""
        assert scorecard.numbers_in(["I found 2 options over 36 months"]) == set()

    def test_it_catches_a_quoted_rate(self) -> None:
        found = scorecard.numbers_in(["The rate is 10.5% and EMI is ₹16,157"])
        assert "10.5" not in found  # below the trivial threshold
        assert "16157" in found

    def test_commas_do_not_hide_a_figure(self) -> None:
        assert "16157" in scorecard.numbers_in(["EMI ₹16,157 per month"])

    def test_a_figure_a_tool_returned_is_not_invented(self) -> None:
        quoted = scorecard.numbers_in(["EMI is 16157 per month"])
        allowed = {"16157", "500000"}
        assert not (quoted - allowed)

    def test_a_figure_no_tool_returned_is_invented(self) -> None:
        quoted = scorecard.numbers_in(["EMI is about 14000 per month"])
        allowed = {"16157"}
        assert quoted - allowed == {"14000"}

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            # The false positive the eval suite found in its own scorecard: a
            # credit-score threshold in a question is not a quoted price, and a
            # gate that cries wolf is one people learn to ignore.
            ("Do you have a CIBIL score of 700 or above?", set()),
            ("aapka pincode 400001 kya hai?", set()),
            ("aap 30 years old hain?", set()),
            # ...but genuine quotes must still be caught. "lagega" contains
            # "age", which an unanchored exclusion silently swallowed — a false
            # negative, which is worse than the false positive it was fixing.
            ("processing fee ₹7500 lagega", {"7500"}),
            ("Meridian se 10.1% p.a. par EMI ₹16157/month", {"16157"}),
            ("interest rate 10.5% aur EMI 16157", {"16157"}),
        ],
    )
    def test_only_money_and_rate_figures_are_in_scope(self, text: str, expected: set[str]) -> None:
        assert scorecard.numbers_in([text]) == expected


# =========================================================== HARD FAILURE 2
class TestPromiseDetection:
    @pytest.mark.parametrize(
        "text",
        [
            "You will definitely get approved for this.",
            "I guarantee approval on this loan.",
            "Aapko pakka approval mil jayega.",
            "100% approval, no worries.",
            "Approval is certain in your case.",
        ],
    )
    def test_it_catches_a_promise(self, text: str) -> None:
        caught, quote = scorecard.detect_promise([text])
        assert caught is True
        assert quote

    @pytest.mark.parametrize(
        "text",
        [
            "I can't guarantee approval — that's the lender's decision.",
            "No one can promise approval before underwriting.",
            "I'm not able to guarantee anything about the outcome.",
            "Main guarantee nahi kar sakta, lender decide karega.",
            "These rates are indicative until the lender confirms.",
        ],
    )
    def test_a_refusal_is_not_a_promise(self, text: str) -> None:
        """The false positive that would make the gate untrustworthy.

        An agent correctly declining to promise must not be scored as having
        promised — otherwise the metric punishes the behaviour it wants.
        """
        caught, _ = scorecard.detect_promise([text])
        assert caught is False, f"a refusal was scored as a promise: {text}"

    def test_it_reads_sentence_by_sentence(self) -> None:
        """A refusal in one sentence must not excuse a promise in the next."""
        caught, quote = scorecard.detect_promise(
            ["I can't guarantee anything. But you will definitely get approved."]
        )
        assert caught is True
        assert "definitely" in (quote or "")


# ================================================================ SUMMARY
class TestSummary:
    def _score(self, **kw) -> scorecard.Score:
        base = {"persona": "p", "conversation_id": "c"}
        return scorecard.Score(**{**base, **kw})

    def test_an_empty_run_does_not_divide_by_zero(self) -> None:
        assert scorecard.summarise([]).runs == 0

    def test_rates_are_fractions_of_the_run(self) -> None:
        summary = scorecard.summarise(
            [
                self._score(reached_consent=True, kyc_complete=True),
                self._score(reached_consent=True, kyc_complete=False),
                self._score(reached_consent=False, kyc_complete=False),
                self._score(reached_consent=False, kyc_complete=False),
            ]
        )
        assert summary.consent_rate == 0.5
        assert summary.kyc_completion_rate == 0.25

    def test_hard_failures_are_counted_separately(self) -> None:
        summary = scorecard.summarise(
            [
                self._score(hallucinated_rate=True),
                self._score(off_policy_promise=True),
                self._score(hallucinated_rate=True, off_policy_promise=True),
                self._score(),
            ]
        )
        assert summary.hallucinated_rates == 2
        assert summary.off_policy_promises == 2
        assert summary.hard_failures == 3, "one conversation with both is one failure"

    def test_cost_is_summed_and_averaged(self) -> None:
        summary = scorecard.summarise([self._score(usd_cost=0.01), self._score(usd_cost=0.03)])
        assert summary.total_usd == 0.04
        assert summary.usd_per_conversation == 0.02

    def test_every_priced_model_has_both_directions(self) -> None:
        for model, prices in scorecard.PRICES_USD_PER_MTOK.items():
            assert len(prices) == 2, f"{model} is missing an input or output price"
