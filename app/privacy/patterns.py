"""Detecting Indian identifiers, and refusing to detect things that merely look like them.

The plan's instruction is "regex plus checksum", and the checksum is the part
that matters. A bare `\\d{4}\\s?\\d{4}\\s?\\d{4}` matches any twelve digits — order
numbers, transaction references, a loan amount someone typed with spaces. Every
false positive is a fragment of an ordinary message silently replaced with a
token, which is both a broken conversation and a vault full of junk.

Aadhaar carries a Verhoeff check digit, so the test is exact. PAN has no
checksum but does have structure: the fourth character encodes holder type and
the tenth is a check letter, which is enough to reject most accidents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Verhoeff — the checksum Aadhaar uses. Dihedral group D5; it catches all
# single-digit errors and all adjacent transpositions, which is why UIDAI chose
# it over a simple mod-10.
# ---------------------------------------------------------------------------
_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


# Inverse of the dihedral group, for generating a check digit rather than
# checking one. Tests need structurally valid synthetic Aadhaars: inventing
# twelve digits produces a number the detector correctly ignores, which makes
# for a test that passes while proving nothing.
_INV = (0, 4, 3, 2, 1, 5, 6, 7, 8, 9)


def verhoeff_digit(payload: str) -> int:
    """The check digit that makes `payload + digit` a valid Verhoeff number."""
    checksum = 0
    for i, digit in enumerate(reversed(payload)):
        checksum = _D[checksum][_P[(i + 1) % 8][int(digit)]]
    return _INV[checksum]


def synthetic_aadhaar(prefix: str = "99998888777") -> str:
    """A structurally valid, obviously fake Aadhaar for tests and demos.

    Real Aadhaars never begin with 0 or 1; this begins with 9s and is not
    issued to anyone.
    """
    return prefix + str(verhoeff_digit(prefix))


def verhoeff_ok(digits: str) -> bool:
    """True if `digits` carries a valid Verhoeff check digit."""
    stripped = re.sub(r"\D", "", digits)
    if not stripped:
        return False
    checksum = 0
    for i, digit in enumerate(reversed(stripped)):
        checksum = _D[checksum][_P[i % 8][int(digit)]]
    return checksum == 0


# ---------------------------------------------------------------------------
# PAN — AAAAA9999A. The fourth character is holder type; anything outside the
# known set is not a PAN, whatever the shape says.
# ---------------------------------------------------------------------------
PAN_HOLDER_TYPES = frozenset("ABCFGHLJPTK")


def pan_ok(candidate: str) -> bool:
    value = candidate.strip().upper()
    if not re.fullmatch(r"[A-Z]{5}[0-9]{4}[A-Z]", value):
        return False
    return value[3] in PAN_HOLDER_TYPES


@dataclass(frozen=True, slots=True)
class Detection:
    kind: str
    value: str
    start: int
    end: int


# Ordered: the first pattern to claim a span wins, and Aadhaar is checked before
# phone because a 12-digit run should not be nibbled at by a 10-digit matcher.
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("PAN", re.compile(r"\b[A-Z]{5}[0-9]{4}[A-Z]\b", re.I)),
    ("AADHAAR", re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b")),
    ("ACCOUNT", re.compile(r"\b\d{11,18}\b")),
    ("PHONE", re.compile(r"\b(?:\+?91[\s-]?)?[6-9]\d{9}\b")),
    ("IFSC", re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")),
)


def _accept(kind: str, value: str) -> bool:
    """The checksum gate. This is what separates detection from guessing."""
    if kind == "AADHAAR":
        # Twelve digits AND a valid check digit. Without the second half this
        # matches order ids, and every match corrupts a real message.
        return verhoeff_ok(value)
    if kind == "PAN":
        return pan_ok(value)
    if kind == "PHONE":
        digits = re.sub(r"\D", "", value)
        # Indian mobiles start 6-9 and are 10 digits, optionally +91 prefixed.
        return len(digits) in (10, 12) and digits[-10] in "6789"
    return True


def detect(text: str) -> list[Detection]:
    """Every identifier in `text`, non-overlapping, longest-claim-first."""
    found: list[Detection] = []
    claimed: list[tuple[int, int]] = []

    for kind, pattern in PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span()
            if any(start < c_end and end > c_start for c_start, c_end in claimed):
                continue
            value = match.group(0)
            if not _accept(kind, value):
                continue
            found.append(Detection(kind=kind, value=value, start=start, end=end))
            claimed.append((start, end))

    found.sort(key=lambda d: d.start)
    return found


def scrub(text: str) -> str:
    """Replace every identifier with a bare `[KIND]`. Irreversible.

    Used for logs, where nothing should ever need the value back.
    """
    out = text
    for detection in reversed(detect(text)):
        out = out[: detection.start] + f"[{detection.kind}]" + out[detection.end :]
    return out
