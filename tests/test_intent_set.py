"""The labelled set, and the scorer that reads it.

A gold set nobody checks drifts, and a drifted gold set reports a number that
measures the labeller rather than the model. These tests are cheap insurance on
the one artifact in this repo that cannot be regenerated from code.
"""

from __future__ import annotations

import collections
from typing import Any

import pytest

from app.graph.prompts import INTENTS
from app.llm import ModelError
from evals import intent_f1
from evals.intent_f1 import Counts

ROWS = intent_f1.load_set()


# ============================================================== THE SET
class TestLabelledSet:
    def test_it_is_the_size_the_plan_asked_for(self) -> None:
        assert len(ROWS) >= 150

    def test_ids_are_unique(self) -> None:
        ids = [r["id"] for r in ROWS]
        assert len(set(ids)) == len(ids)

    def test_every_intent_is_in_the_taxonomy(self) -> None:
        """A label outside the enum can never be predicted, so it scores zero forever."""
        for row in ROWS:
            assert row["intent"] in INTENTS, f"id {row['id']}: {row['intent']}"

    def test_every_intent_has_examples(self) -> None:
        """An intent with no examples is an untested branch of the taxonomy."""
        seen = collections.Counter(r["intent"] for r in ROWS)
        missing = [i for i in INTENTS if seen[i] == 0]
        assert not missing, f"no labelled examples for: {missing}"

    def test_all_three_scripts_are_represented(self) -> None:
        """ "Handles Devanagari" needs Devanagari in the set to be a claim at all."""
        counts = collections.Counter(r["script"] for r in ROWS)
        assert counts["latin"] >= 40
        assert counts["devanagari"] >= 40
        assert counts["mixed"] >= 30

    def test_devanagari_rows_actually_contain_devanagari(self) -> None:
        for row in ROWS:
            if row["script"] == "devanagari":
                assert any("ऀ" <= c <= "ॿ" for c in row["text"]), row["id"]

    def test_mixed_rows_contain_both_scripts(self) -> None:
        for row in ROWS:
            if row["script"] == "mixed":
                has_deva = any("ऀ" <= c <= "ॿ" for c in row["text"])
                has_latin = any(c.isascii() and c.isalpha() for c in row["text"])
                assert has_deva and has_latin, f"id {row['id']} is not mixed"

    def test_every_slot_key_is_scorable(self) -> None:
        """A label for a slot the scorer ignores is silently worthless."""
        for row in ROWS:
            for key in row["slots"]:
                assert key in intent_f1.SCORED_SLOTS, f"id {row['id']}: {key}"

    def test_slot_values_are_in_range(self) -> None:
        allowed = {
            "product": {"personal_loan", "home_loan", "business_loan", "gold_loan"},
            "income_band": {"under_25k", "25k_50k", "50k_1l", "above_1l"},
            "pan_status": {"available", "missing"},
        }
        for row in ROWS:
            for key, values in allowed.items():
                if key in row["slots"]:
                    assert row["slots"][key] in values, f"id {row['id']}: {key}"
            if "amount_inr" in row["slots"]:
                assert isinstance(row["slots"]["amount_inr"], int)
                assert row["slots"]["amount_inr"] >= 1000

    def test_every_row_carries_a_stage(self) -> None:
        """The extractor always has one in production; scoring without it is unfaithful."""
        for row in ROWS:
            assert row.get("stage"), f"id {row['id']} has no stage"

    def test_naming_a_product_means_product_enquiry(self) -> None:
        """The precedence rule in LABELLING.md, enforced.

        Five rows broke it before the rule was written down, and an inconsistent
        gold set penalises a model for the labeller's indecision.
        """
        for row in ROWS:
            if "product" in row["slots"] and row["intent"] not in (
                "opt_out",
                "escalation_request",
                "objection",
            ):
                assert row["intent"] == "product_enquiry", (
                    f"id {row['id']} names a product but is labelled {row['intent']}"
                )

    def test_an_opt_out_is_labelled_opt_out_even_with_an_objection(self) -> None:
        for row in ROWS:
            if row["slots"].get("opted_out"):
                assert row["intent"] == "opt_out", f"id {row['id']}"


# ============================================================== THE SCORER
class TestCounts:
    def test_perfect(self) -> None:
        c = Counts(tp=10)
        assert c.precision == 1.0 and c.recall == 1.0 and c.f1 == 1.0

    def test_nothing_predicted_is_zero_not_a_crash(self) -> None:
        c = Counts(fn=5)
        assert c.precision == 0.0 and c.recall == 0.0 and c.f1 == 0.0

    def test_empty_is_zero_not_a_crash(self) -> None:
        assert Counts().f1 == 0.0

    def test_f1_is_the_harmonic_mean(self) -> None:
        c = Counts(tp=6, fp=2, fn=4)  # P 0.75, R 0.60
        assert c.precision == pytest.approx(0.75)
        assert c.recall == pytest.approx(0.60)
        assert c.f1 == pytest.approx(2 * 0.75 * 0.6 / 1.35)

    def test_support_counts_gold_not_predictions(self) -> None:
        assert Counts(tp=3, fp=99, fn=2).support == 5


class TestEquality:
    def test_amounts_compare_numerically(self) -> None:
        assert intent_f1._same("amount_inr", 500000, "500000")
        assert not intent_f1._same("amount_inr", 500000, 50000)

    def test_a_non_numeric_amount_is_not_a_match(self) -> None:
        assert not intent_f1._same("amount_inr", 500000, "five lakh")

    @pytest.mark.parametrize(
        ("gold", "got"),
        [
            ("interest_rate", "rate"),
            ("interest_rate", "interest"),
            ("fees", "processing fees"),
            ("fees", "processing_fee"),
        ],
    )
    def test_objection_labels_are_compared_loosely(self, gold: str, got: str) -> None:
        """Otherwise the metric measures vocabulary rather than understanding."""
        assert intent_f1._same("objection", gold, got)

    def test_a_genuinely_different_objection_is_still_wrong(self) -> None:
        assert not intent_f1._same("objection", "interest_rate", "timing")

    def test_everything_else_is_exact(self) -> None:
        assert intent_f1._same("pan_status", "available", "available")
        assert not intent_f1._same("pan_status", "available", "missing")
        assert not intent_f1._same("income_band", "50k_1l", "25k_50k")


class TestSchema:
    def test_intent_is_required(self) -> None:
        """An optional intent is silently absent, and absent scores as wrong.

        It cost a whole comparison run: the few-shot arm omitted `intent` on 33
        of 150 messages and lost by 8.7 points, most of which was the schema
        rather than the prompt.
        """
        from app.graph.prompts import EXTRACTION_SCHEMA, PROMPT_GATED_SCHEMA

        assert "intent" in EXTRACTION_SCHEMA["required"]
        assert "intent" in PROMPT_GATED_SCHEMA["required"]

    def test_no_slot_is_required(self) -> None:
        """The opposite rule, for everything else: "not stated" must stay sayable."""
        from app.graph.prompts import EXTRACTION_SCHEMA

        assert set(EXTRACTION_SCHEMA["required"]) == {"intent"}


class TestOutageGuard:
    """A provider that stops answering must abort, not report a bad score."""

    @staticmethod
    async def _run_with(monkeypatch: pytest.MonkeyPatch, failures: int) -> intent_f1.Result:
        calls = {"n": 0}

        async def fake_extract(**_: Any) -> Any:
            calls["n"] += 1
            if calls["n"] <= failures:
                raise ModelError("429: quota exceeded")
            return type("R", (), {"data": {"intent": "greeting"}})()

        monkeypatch.setattr(
            intent_f1,
            "get_provider",
            lambda: type("P", (), {"extract": staticmethod(fake_extract)})(),
        )
        return await intent_f1.run("rules", limit=12)

    async def test_a_burst_of_failures_aborts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(intent_f1.RunAborted, match="refusing"):
            await self._run_with(monkeypatch, failures=intent_f1.MAX_CONSECUTIVE_ERRORS)

    async def test_isolated_failures_are_scored_as_misses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One flake is noise. Aborting on it would make the eval unrunnable."""
        result = await self._run_with(monkeypatch, failures=1)
        assert result.errors == 1
        assert result.total == 12
