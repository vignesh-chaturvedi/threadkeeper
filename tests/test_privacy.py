"""Detection, checksums and scrubbing — no database, no crypto, no model."""

from __future__ import annotations

import pytest

from app.privacy import patterns
from app.privacy.patterns import detect, pan_ok, scrub, synthetic_aadhaar, verhoeff_ok

AADHAAR = synthetic_aadhaar()  # structurally valid, issued to nobody


# ============================================================== VERHOEFF
class TestVerhoeff:
    def test_known_vectors(self) -> None:
        assert verhoeff_ok("2363") is True
        assert verhoeff_ok("2364") is False

    def test_a_generated_check_digit_validates(self) -> None:
        for prefix in ("99998888777", "12345678901", "40000000000"):
            assert verhoeff_ok(prefix + str(patterns.verhoeff_digit(prefix)))

    def test_it_catches_every_single_digit_error(self) -> None:
        """The property UIDAI chose Verhoeff for. Worth asserting, not assuming."""
        for position in range(len(AADHAAR)):
            for replacement in "0123456789":
                if replacement == AADHAAR[position]:
                    continue
                broken = AADHAAR[:position] + replacement + AADHAAR[position + 1 :]
                assert not verhoeff_ok(broken), f"missed a typo at {position}"

    def test_it_catches_adjacent_transpositions(self) -> None:
        """The other property. A plain mod-10 checksum misses all of these."""
        caught = 0
        for i in range(len(AADHAAR) - 1):
            if AADHAAR[i] == AADHAAR[i + 1]:
                continue
            swapped = AADHAAR[:i] + AADHAAR[i + 1] + AADHAAR[i] + AADHAAR[i + 2 :]
            assert not verhoeff_ok(swapped), f"missed a transposition at {i}"
            caught += 1
        assert caught > 0

    def test_empty_is_not_valid(self) -> None:
        assert verhoeff_ok("") is False
        assert verhoeff_ok("abc") is False


# ================================================================== PAN
class TestPan:
    @pytest.mark.parametrize("value", ["ABCPE1234F", "abcpe1234f", "AAACH1234C"])
    def test_valid_shapes(self, value: str) -> None:
        assert pan_ok(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "ABCDE1234F",  # 'D' is not a holder type
            "ABCP12345",  # too short
            "ABCPE12345",  # last char must be a letter
            "12345ABCDE",
            "",
        ],
    )
    def test_invalid_shapes(self, value: str) -> None:
        assert pan_ok(value) is False


# ============================================================== DETECTION
class TestDetection:
    def test_finds_all_three_in_one_message(self) -> None:
        text = f"mera PAN ABCPE1234F hai, aadhaar {AADHAAR} aur number 9876543210"
        kinds = {d.kind for d in detect(text)}
        assert kinds == {"PAN", "AADHAAR", "PHONE"}

    @pytest.mark.parametrize(
        "text",
        [
            "order 1234 5678 9012 aaya",  # twelve digits, bad checksum
            "amount 500000 chahiye",
            "5 lakh chahiye",
            "ref ZZZZZ1234Z",  # PAN shape, invalid holder type
            "meeting at 9876 tomorrow",
        ],
    )
    def test_things_that_only_look_like_identifiers_are_left_alone(self, text: str) -> None:
        """Every false positive silently corrupts a real message."""
        assert detect(text) == []

    def test_a_bad_checksum_aadhaar_is_not_an_aadhaar(self) -> None:
        """The whole argument for 'regex plus checksum' rather than regex."""
        broken = AADHAAR[:-1] + str((int(AADHAAR[-1]) + 1) % 10)
        assert not any(d.kind == "AADHAAR" for d in detect(broken))

    def test_spaced_and_hyphenated_aadhaar_both_match(self) -> None:
        spaced = f"{AADHAAR[:4]} {AADHAAR[4:8]} {AADHAAR[8:]}"
        hyphened = f"{AADHAAR[:4]}-{AADHAAR[4:8]}-{AADHAAR[8:]}"
        for form in (spaced, hyphened):
            assert any(d.kind == "AADHAAR" for d in detect(f"mera aadhaar {form} hai"))

    def test_detections_never_overlap(self) -> None:
        text = f"PAN ABCPE1234F aadhaar {AADHAAR} phone +91 9876543210"
        spans = [(d.start, d.end) for d in detect(text)]
        for i, (start, end) in enumerate(spans):
            for other_start, other_end in spans[i + 1 :]:
                assert end <= other_start or other_end <= start

    def test_a_twelve_digit_run_is_not_nibbled_by_the_phone_matcher(self) -> None:
        """Ordering matters: Aadhaar must claim the span before PHONE sees it."""
        found = detect(f"aadhaar {AADHAAR}")
        assert [d.kind for d in found] == ["AADHAAR"]


# ================================================================= SCRUB
class TestScrub:
    def test_replaces_every_identifier(self) -> None:
        text = f"PAN ABCPE1234F, aadhaar {AADHAAR}, phone 9876543210"
        out = scrub(text)
        assert "ABCPE1234F" not in out
        assert AADHAAR not in out
        assert "9876543210" not in out
        assert out.count("[PAN]") == 1
        assert out.count("[AADHAAR]") == 1
        assert out.count("[PHONE]") == 1

    def test_leaves_ordinary_text_untouched(self) -> None:
        text = "bhai 5 lakh ka personal loan chahiye, salary 60k hai"
        assert scrub(text) == text

    def test_is_idempotent(self) -> None:
        text = f"PAN ABCPE1234F and aadhaar {AADHAAR}"
        assert scrub(scrub(text)) == scrub(text)
