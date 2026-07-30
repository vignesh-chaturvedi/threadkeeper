"""The three tiers, and the conflict rule.

Most of this needs no database and no model: the token estimator, the profile
renderer and the conflict rule are all pure functions, which is the same
argument the stage policy makes — the parts that decide things should be
testable without infrastructure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.memory import conflict, profile, tokens


# ===========================================================================
# tier 1 — token budgeting
# ===========================================================================
class TestTokenEstimator:
    @pytest.mark.parametrize(
        ("text", "real"),
        [
            # Measured against Gemini's countTokens endpoint. See
            # evals/calibrate_tokens.py — these are recorded, not invented.
            ("I need a personal loan", 5),
            ("hi", 1),
            ("haan theek hai", 4),
            ("bhai mujhe 5 lakh ka personal loan chahiye, salary 60k hai, PAN nahi hai abhi", 23),
            ("मुझे पाँच लाख रुपये का व्यक्तिगत ऋण चाहिए, ब्याज दर क्या होगी", 13),
            ('{"product":"personal_loan","amount_inr":500000,"income_band":"50k_1l"}', 30),
            ("aaj mausam accha hai na", 8),
        ],
    )
    def test_never_under_estimates(self, text: str, real: int) -> None:
        """The asymmetry that matters.

        Over-estimating trims a little extra history — nobody notices.
        Under-estimating overflows the context window mid-conversation.
        """
        assert tokens.estimate_tokens(text) >= real, f"under-estimated {text!r}"

    def test_punctuation_is_charged_for(self) -> None:
        """The JSON case: same length, far more tokens."""
        prose = "the quick brown fox jumped over it"
        dense = '{"a":1,"b":2,"c":3,"d":4,"e":5,"f":6}'
        assert len(dense) <= len(prose) + 4
        assert tokens.estimate_tokens(dense) > tokens.estimate_tokens(prose)

    def test_empty_is_free_but_anything_is_at_least_one(self) -> None:
        assert tokens.estimate_tokens("") == 0
        assert tokens.estimate_tokens("a") >= 1


class TestBudget:
    def _msgs(self, *texts: str) -> list[dict[str, str]]:
        return [{"text": t} for t in texts]

    def test_keeps_the_newest_and_drops_the_oldest(self) -> None:
        msgs = self._msgs("a" * 300, "b" * 300, "newest")
        kept = tokens.fit_to_budget(msgs, 60, text_of=lambda m: m["text"])
        assert kept[-1]["text"] == "newest"
        assert len(kept) < len(msgs)

    def test_order_is_preserved(self) -> None:
        msgs = self._msgs("one", "two", "three")
        kept = tokens.fit_to_budget(msgs, 1000, text_of=lambda m: m["text"])
        assert [m["text"] for m in kept] == ["one", "two", "three"]

    def test_one_enormous_message_still_comes_back(self) -> None:
        """Dropping the customer's actual turn would be worse than overflowing."""
        msgs = self._msgs("x" * 10_000)
        kept = tokens.fit_to_budget(msgs, 10, text_of=lambda m: m["text"])
        assert len(kept) == 1

    def test_trimming_is_by_tokens_not_count(self) -> None:
        """The whole point of tier 1: ten short messages beat two enormous ones."""
        short = tokens.fit_to_budget(self._msgs(*["hi"] * 10), 200, text_of=lambda m: m["text"])
        long = tokens.fit_to_budget(self._msgs(*["y" * 400] * 10), 200, text_of=lambda m: m["text"])
        assert len(short) == 10
        assert len(long) < 3

    def test_empty_input(self) -> None:
        assert tokens.fit_to_budget([], 100, text_of=lambda m: m["text"]) == []


# ===========================================================================
# tier 2 — the structured profile
# ===========================================================================
class TestProfile:
    def test_renders_known_facts_compactly(self) -> None:
        block = profile.render(
            {"product": "personal_loan", "amount_inr": 500_000, "income_band": "50k_1l"}
        )
        assert "personal loan" in block
        assert "5 lakh" in block
        assert "50k-1L/month" in block

    def test_omits_what_is_not_known(self) -> None:
        block = profile.render({"product": "home_loan"})
        assert "PAN" not in block
        assert "income" not in block

    def test_nothing_known_renders_empty(self) -> None:
        assert profile.render({}) == ""

    def test_is_deterministic_regardless_of_insertion_order(self) -> None:
        """A prompt hash is only meaningful if the same facts render identically."""
        a = profile.render({"product": "personal_loan", "pan_status": "available"})
        b = profile.render({"pan_status": "available", "product": "personal_loan"})
        assert a == b

    def test_is_cheaper_than_the_json_it_replaces(self) -> None:
        """Measured, because this was the reason for rendering lines not JSON."""
        import json

        slots = {
            "product": "personal_loan",
            "amount_inr": 500_000,
            "income_band": "50k_1l",
            "pan_status": "available",
            "city_tier": 2,
        }
        as_lines = tokens.estimate_tokens(profile.render(slots))
        as_json = tokens.estimate_tokens(json.dumps(slots, sort_keys=True))
        assert as_lines < as_json, f"lines={as_lines} json={as_json}"

    def test_consent_is_rendered_as_a_decision(self) -> None:
        block = profile.render({}, {"granted": True, "wording_hash": "abc"})
        assert "consent" in block and "given" in block


# ===========================================================================
# the conflict rule
# ===========================================================================
class TestConflictRule:
    def _v(self, value, source="extracted", ago_s=0):
        return conflict.SlotValue(
            value=value,
            source=source,
            updated_at=datetime.now(UTC) - timedelta(seconds=ago_s),
        )

    def test_first_value_is_taken(self) -> None:
        out = conflict.resolve("amount_inr", None, self._v(400_000))
        assert out.winner.value == 400_000 and out.changed

    def test_a_correction_wins(self) -> None:
        """Customer said 4 lakh, now says 6. That is the plan's example."""
        out = conflict.resolve("amount_inr", self._v(400_000, ago_s=60), self._v(600_000))
        assert out.winner.value == 600_000
        assert out.reason == "newer_same_provenance"

    def test_confirmed_beats_a_newer_extraction(self) -> None:
        """The plan's key decision, stated the other way round."""
        confirmed = self._v("available", source="confirmed", ago_s=600)
        guessed = self._v("missing", source="extracted", ago_s=0)
        out = conflict.resolve("pan_status", confirmed, guessed)
        assert out.winner.value == "available"
        assert out.reason == "weaker_provenance_rejected"

    def test_a_newer_confirmation_beats_an_older_extraction(self) -> None:
        out = conflict.resolve(
            "pan_status",
            self._v("missing", source="extracted", ago_s=600),
            self._v("available", source="confirmed"),
        )
        assert out.winner.value == "available"
        assert out.reason == "stronger_provenance"

    def test_an_opt_out_does_not_lapse(self) -> None:
        """Silence is not re-consent. This one has legal weight."""
        out = conflict.resolve("opted_out", self._v(True), self._v(False))
        assert out.winner.value is True
        assert out.reason == "sticky_not_downgraded"

    def test_consent_cannot_be_quietly_revoked_by_an_extraction(self) -> None:
        granted = self._v({"granted": True}, source="confirmed")
        out = conflict.resolve("consent", granted, self._v({"granted": False}))
        assert out.winner.value["granted"] is True

    def test_consent_can_be_withdrawn_by_a_confirmation(self) -> None:
        """Revocable means revocable — by an equally strong signal."""
        granted = self._v({"granted": True}, source="confirmed", ago_s=600)
        withdrawn = self._v({"granted": False}, source="confirmed")
        out = conflict.resolve("consent", granted, withdrawn)
        assert out.winner.value["granted"] is False

    def test_an_empty_incoming_value_changes_nothing(self) -> None:
        out = conflict.resolve("product", self._v("personal_loan"), self._v(None))
        assert out.winner.value == "personal_loan" and not out.changed

    def test_the_same_value_from_a_better_source_is_an_upgrade(self) -> None:
        """It changes what a later weak observation is allowed to do."""
        out = conflict.resolve(
            "pan_status",
            self._v("available", source="extracted"),
            self._v("available", source="confirmed"),
        )
        assert out.reason == "provenance_upgraded" and out.changed

    def test_an_older_value_never_overwrites_a_newer_one(self) -> None:
        out = conflict.resolve("amount_inr", self._v(600_000, ago_s=0), self._v(400_000, ago_s=600))
        assert out.winner.value == 600_000
        assert out.reason == "older_rejected"

    def test_source_ranking_is_the_documented_order(self) -> None:
        assert (
            conflict.SOURCE_RANK["extracted"]
            < conflict.SOURCE_RANK["api"]
            < conflict.SOURCE_RANK["confirmed"]
        )

    def test_an_existing_value_without_a_timestamp_is_treated_as_older(self) -> None:
        """Regression: this made every correction fail.

        State carries slot values but not when each was learned. Defaulting the
        missing timestamp to `now()` made the stored value always look newer
        than the one arriving, so "4 lakh, sorry, 6 lakh" silently kept 4.
        """
        stored = conflict.SlotValue(value=400_000, source="extracted")  # no updated_at
        incoming = self._v(600_000)
        out = conflict.resolve("amount_inr", stored, incoming)
        assert out.winner.value == 600_000
        assert out.changed
