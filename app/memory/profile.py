"""Tier 2 — the structured profile.

The opinion this project holds, stated plainly: in a sales funnel, most of what
matters is **slot-filling, not vector search**. Retrieval can tell you what a
customer complained about last time. It cannot reliably tell you their income
band, and if it could you still should not ask it to, because the answer would
be approximate and unauditable.

So tier 2 is a `SELECT` against a table, rendered as a handful of compact lines.
Cheap, exact, and debuggable — you can read the exact string that went into the
prompt, and it is the same string every time for the same facts.

Rendered as lines rather than JSON on purpose: the calibration in `tokens.py`
found JSON-shaped text runs at 2.33 chars/token against 4.4 for prose, so the
brace-and-quote version of the same facts costs nearly twice as much.
"""

from __future__ import annotations

from typing import Any

# Rendered in this order regardless of insertion order, so the same facts always
# produce the same bytes — which is what makes a prompt hash meaningful.
FIELD_ORDER = (
    "product",
    "amount_inr",
    "income_band",
    "city_tier",
    "pan_status",
    "objection",
    "opted_out",
)

LABELS = {
    "product": "wants",
    "amount_inr": "amount",
    "income_band": "income",
    "city_tier": "city tier",
    "pan_status": "PAN",
    "objection": "pushed back on",
    "opted_out": "opted out",
}


# Explicit, because a generic string transform produced en-dashes that ruff
# flags as ambiguous — and a lookup reads better than three chained replaces.
INCOME_BANDS = {
    "under_25k": "under 25k/month",
    "25k_50k": "25k-50k/month",
    "50k_1l": "50k-1L/month",
    "above_1l": "above 1L/month",
}


def _fmt_amount(value: Any) -> str:
    try:
        rupees = int(value)
    except (TypeError, ValueError):
        return str(value)
    if rupees >= 100_000 and rupees % 100_000 == 0:
        return f"{rupees // 100_000} lakh"
    return f"₹{rupees:,}"


def _fmt(key: str, value: Any) -> str:
    if key == "amount_inr":
        return _fmt_amount(value)
    if key == "product":
        return str(value).replace("_", " ")
    if key == "income_band":
        return INCOME_BANDS.get(str(value), str(value))
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def render(slots: dict[str, Any], consent: dict[str, Any] | None = None) -> str:
    """Compact facts for the prompt. Empty string when nothing is known yet."""
    lines: list[str] = []

    for key in FIELD_ORDER:
        value = slots.get(key)
        if value in (None, "", []):
            continue
        lines.append(f"- {LABELS.get(key, key)}: {_fmt(key, value)}")

    consent = consent or slots.get("consent") or {}
    if isinstance(consent, dict) and "granted" in consent:
        verdict = "given" if consent["granted"] else "refused"
        lines.append(f"- consent to share with lenders: {verdict}")

    return "\n".join(lines)


def known_keys(slots: dict[str, Any]) -> list[str]:
    return [k for k in FIELD_ORDER if slots.get(k) not in (None, "", [])]


def missing_keys(slots: dict[str, Any], required: tuple[str, ...]) -> list[str]:
    return [k for k in required if slots.get(k) in (None, "", [])]
