"""A mock lender marketplace.

Everything here is invented, and deliberately so — this project has no lender
partnership and never will. What matters is that it behaves like a real
integration in the ways that break agents:

  * **It is deterministic.** The same inputs always produce the same offer, so
    an eval run is reproducible and `create_application` can verify that an
    offer id was genuinely quoted rather than hallucinated.
  * **It is slow sometimes, and fails sometimes.** Latency and a configurable
    failure rate are injected on purpose. An agent that only ever sees a fast,
    successful lender is an agent whose degradation path has never run.
  * **It refuses.** Eligibility rules say no, and the agent has to handle no.

Rates are computed from a matrix, never sampled. That is what makes "the agent
must never invent a rate" a checkable property in Phase 08: every number in a
reply either came from here or is a hallucination.
"""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any

from app.logging import get_logger
from app.settings import get_settings

log = get_logger(__name__)

INCOME_RANK = {"under_25k": 0, "25k_50k": 1, "50k_1l": 2, "above_1l": 3}


class LenderTimeout(Exception):
    """The lender did not answer in time. Retryable."""


class LenderUnavailable(Exception):
    """The lender returned an error. Retryable, but not immediately."""


@dataclass(frozen=True, slots=True)
class Lender:
    id: str
    name: str
    products: frozenset[str]
    min_income: str
    min_amount: int
    max_amount: int
    city_tiers: frozenset[int]
    base_apr: float
    processing_fee_pct: float
    requires_pan: bool = True


CATALOGUE: tuple[Lender, ...] = (
    Lender(
        id="meridian",
        name="Meridian Finance",
        products=frozenset({"personal_loan", "business_loan"}),
        min_income="25k_50k",
        min_amount=50_000,
        max_amount=1_500_000,
        city_tiers=frozenset({1, 2}),
        base_apr=10.75,
        processing_fee_pct=1.5,
    ),
    Lender(
        id="sahaj",
        name="Sahaj Credit",
        products=frozenset({"personal_loan", "gold_loan"}),
        min_income="under_25k",
        min_amount=25_000,
        max_amount=500_000,
        city_tiers=frozenset({1, 2, 3}),
        base_apr=14.25,
        processing_fee_pct=2.0,
    ),
    Lender(
        id="anchor",
        name="Anchor Housing",
        products=frozenset({"home_loan"}),
        min_income="50k_1l",
        min_amount=500_000,
        max_amount=15_000_000,
        city_tiers=frozenset({1, 2}),
        base_apr=8.60,
        processing_fee_pct=0.5,
    ),
    Lender(
        id="tarafirst",
        name="Tara First",
        products=frozenset({"personal_loan", "business_loan", "gold_loan"}),
        min_income="under_25k",
        min_amount=20_000,
        max_amount=300_000,
        city_tiers=frozenset({2, 3}),
        base_apr=17.50,
        processing_fee_pct=2.5,
        requires_pan=False,
    ),
)


@dataclass(slots=True)
class Ineligibility:
    lender: str
    reason: str


def _eligible(
    lender: Lender, product: str, income_band: str, city_tier: int, amount_inr: int
) -> str | None:
    """Returns a refusal reason, or None if they would lend."""
    if product not in lender.products:
        return "product_not_offered"
    if INCOME_RANK.get(income_band, -1) < INCOME_RANK[lender.min_income]:
        return "income_below_minimum"
    if city_tier not in lender.city_tiers:
        return "city_not_serviced"
    if amount_inr < lender.min_amount:
        return "amount_below_minimum"
    if amount_inr > lender.max_amount:
        return "amount_above_maximum"
    return None


def _apr(lender: Lender, income_band: str, city_tier: int, amount_inr: int) -> float:
    """Deterministic. A sampled rate would make hallucination unfalsifiable."""
    apr = lender.base_apr
    apr -= 0.5 * (INCOME_RANK.get(income_band, 0) - INCOME_RANK[lender.min_income])
    apr += 0.35 * (city_tier - 1)
    if amount_inr >= 1_000_000:
        apr -= 0.4
    elif amount_inr < 100_000:
        apr += 0.75
    return round(max(apr, 6.5), 2)


def _emi(principal: int, apr: float, months: int) -> int:
    r = apr / 12 / 100
    if r == 0:
        return round(principal / months)
    factor = (1 + r) ** months
    return round(principal * r * factor / (factor - 1))


def _offer_id(lender: str, product: str, amount: int, months: int, apr: float) -> str:
    """Stable across calls, so a quoted offer can be verified at application."""
    raw = f"{lender}|{product}|{amount}|{months}|{apr}"
    return "off_" + hashlib.sha256(raw.encode()).hexdigest()[:12]


def _tenure_for(product: str) -> int:
    return {"home_loan": 240, "business_loan": 48, "gold_loan": 12}.get(product, 36)


async def _inject_faults(operation: str) -> None:
    """Latency and failures, on purpose.

    The plan asks for timeouts about 5% of the time. An agent whose lender never
    times out has a degradation path that has never executed once.
    """
    settings = get_settings()

    if settings.lender_latency_ms > 0:
        jitter = secrets.randbelow(max(1, settings.lender_latency_ms))
        await asyncio.sleep((settings.lender_latency_ms + jitter) / 1000)

    if settings.lender_failure_rate > 0:
        roll = secrets.randbelow(10_000) / 10_000
        if roll < settings.lender_failure_rate:
            log.warning("lender_fault_injected", operation=operation)
            # Two thirds timeouts, one third hard errors — timeouts are the more
            # common real failure and the more awkward one to handle.
            if secrets.randbelow(3) < 2:
                raise LenderTimeout(f"{operation} timed out")
            raise LenderUnavailable(f"{operation} returned 503")


# ---------------------------------------------------------------------------
# the API the tools call
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class EligibilityResult:
    eligible: bool
    lenders: list[str] = field(default_factory=list)
    refusals: list[dict[str, str]] = field(default_factory=list)


async def check_eligibility(
    product: str, income_band: str, city_tier: int, amount_inr: int
) -> EligibilityResult:
    await _inject_faults("check_eligibility")

    eligible: list[str] = []
    refusals: list[dict[str, str]] = []
    for lender in CATALOGUE:
        reason = _eligible(lender, product, income_band, city_tier, amount_inr)
        if reason is None:
            eligible.append(lender.id)
        else:
            refusals.append({"lender": lender.name, "reason": reason})

    return EligibilityResult(eligible=bool(eligible), lenders=eligible, refusals=refusals)


async def match(
    product: str, income_band: str, city_tier: int, amount_inr: int, limit: int = 3
) -> list[dict[str, Any]]:
    """Ranked offers. Cheapest APR first."""
    await _inject_faults("fetch_offers")

    months = _tenure_for(product)
    offers: list[dict[str, Any]] = []

    for lender in CATALOGUE:
        if _eligible(lender, product, income_band, city_tier, amount_inr) is not None:
            continue
        apr = _apr(lender, income_band, city_tier, amount_inr)
        offers.append(
            {
                "offer_id": _offer_id(lender.id, product, amount_inr, months, apr),
                "lender_id": lender.id,
                "lender": lender.name,
                "product": product,
                "amount_inr": amount_inr,
                "apr_pct": apr,
                "tenure_months": months,
                "emi_inr": _emi(amount_inr, apr, months),
                "processing_fee_inr": round(amount_inr * lender.processing_fee_pct / 100),
                "requires_pan": lender.requires_pan,
            }
        )

    offers.sort(key=lambda o: o["apr_pct"])
    return offers[:limit]


PAN_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"


async def verify_pan(pan: str) -> dict[str, Any]:
    """Structural validation only.

    A real integration would call NSDL. This checks the format that a real PAN
    has — five letters, four digits, a letter — and nothing else. It never
    returns the number, and the caller masks it before anything is recorded.
    """
    await _inject_faults("verify_pan")

    candidate = (pan or "").strip().upper()
    valid = (
        len(candidate) == 10
        and all(c in PAN_ALPHABET for c in candidate[:5])
        and candidate[5:9].isdigit()
        and candidate[9] in PAN_ALPHABET
    )
    # The fourth character encodes holder type: P individual, C company, H HUF.
    holder = {"P": "individual", "C": "company", "H": "huf", "F": "firm"}.get(
        candidate[3] if valid else "", "unknown"
    )
    return {"verified": valid, "holder_type": holder if valid else None}


async def apply(offer: dict[str, Any], idem_key: str) -> dict[str, Any]:
    """Submit an application to the lender. Idempotency is the caller's job."""
    await _inject_faults("create_application")

    application_id = "app_" + hashlib.sha256(idem_key.encode()).hexdigest()[:14]
    return {
        "application_id": application_id,
        "lender": offer["lender"],
        "status": "submitted",
        "next_step": "The lender will contact you to complete documentation.",
    }
