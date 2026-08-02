"""What a turn cost, in dollars.

One price list for the whole project. It lived in `evals/scorecard.py` first,
which was fine while evals were the only thing counting money — but the console
now reports cost per conversation to the same person who reads the eval output,
and two tables that disagree is worse than no table at all.

Prices are USD per million tokens, input and output, from the published rate
card. Output costs roughly eight times input on these models, which is why the
reply prompt is capped at 320 tokens and the extraction call returns fields
rather than prose.
"""

from __future__ import annotations

from decimal import Decimal

PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.6-flash": (0.30, 2.50),
    # Not a discount — the fake provider makes no network call at all. Pricing it
    # at zero is what keeps CI honest about the difference between "cheap" and
    # "not actually running the model".
    "fake": (0.0, 0.0),
}

# An unknown model prices at zero rather than raising. A pricing gap should not
# be able to fail a customer's turn — but it must be visible, so callers can ask.
UNKNOWN = (0.0, 0.0)


def is_priced(model: str) -> bool:
    """False for a model nobody has entered a price for. Cost then reads 0."""
    return model in PRICES_USD_PER_MTOK


def usd_for(model: str, tokens_in: int, tokens_out: int) -> Decimal:
    """Cost of one call. Decimal, because these are money and they get summed.

    A turn costs about $0.00007. Accumulating float rounding across a few
    thousand of those is how a unit-economics number quietly drifts.
    """
    price_in, price_out = PRICES_USD_PER_MTOK.get(model, UNKNOWN)
    return (
        Decimal(str(price_in)) * Decimal(tokens_in) + Decimal(str(price_out)) * Decimal(tokens_out)
    ) / Decimal(1_000_000)
