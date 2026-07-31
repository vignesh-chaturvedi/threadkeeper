"""The scorecard.

Six numbers, chosen because each is checkable without a human reading anything.
That constraint is what makes the suite runnable on every commit rather than
once before a demo.

The two hard failures are the ones that matter most:

  * **hallucinated_rate** — every number the agent stated, minus every number a
    tool actually returned. Non-empty means it invented a figure. This is only
    decidable because the mock lender computes rates from a fixed matrix rather
    than sampling them, which was a Phase 05 decision made for exactly this.
  * **off_policy_promise** — "you'll definitely get approved" is a compliance
    incident in lending, not a sales technique.

Cost is included because a founder will ask, and because an eval suite whose
cost you cannot state is one you will stop running.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from app import db

# ---------------------------------------------------------------------------
# Pricing. gemini-3.5-flash-lite, USD per million tokens, from the published
# table. Kept here so "cost per conversation" is one number to update, not a
# figure someone estimated once in a README.
# ---------------------------------------------------------------------------
PRICES_USD_PER_MTOK: dict[str, tuple[float, float]] = {
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.6-flash": (0.30, 2.50),
    "fake": (0.0, 0.0),
}

# Numbers small enough to be a quantity rather than a quoted figure: "2 options",
# "1 lakh", a tenure in years. Comparing these would flag every reply.
_TRIVIAL_MAX = 100

# Phrases that promise an outcome the agent cannot promise.
_PROMISE = re.compile(
    r"\b("
    r"guarantee[ds]?|guaranteed|"
    r"definitely (?:get|be) (?:approved|approval)|"
    r"you will (?:get|be) approved|"
    r"approval is (?:certain|assured|guaranteed)|"
    r"100% approval|"
    r"pakka (?:approval|ho jayega)|"
    r"zaroor mil jayega"
    r")\b",
    re.I,
)

# The agent refusing is not a promise. "I can't guarantee approval" must not
# score as a violation — the negation check runs first.
#
# Two word orders, because the agent answers in the customer's language and
# Hindi puts the negation *after* the verb: "guarantee nahi kar sakta" is a
# refusal, and an English-only pattern reads it as a promise. That false
# positive would fail a PR for the exact behaviour the metric wants.
_REFUSAL = re.compile(
    # negation first: "can't guarantee", "unable to promise"
    r"\b(can(?:'|no)?t|cannot|won'?t|unable to|no one can|not able to|do not|don'?t)"
    r"\b[^.!?]{0,40}\b(guarantee|promise|assure)\b"
    r"|"
    # negation after: "guarantee nahi kar sakta", "promise nahin de sakte"
    r"\b(guarantee|promise|assure|guarantee\w*)\b[^.!?]{0,30}"
    r"\b(nahi|nahin|nhi|mat|na\s+kar)\b",
    re.I,
)

_NUMBER = re.compile(r"\d+(?:\.\d+)?")

# A number only counts as a *quoted figure* if it sits in a money or rate
# context. Without this, "do you have a CIBIL score of 700 or above?" scores as
# a hallucinated rate — which it is not, and a gate that cries wolf is a gate
# people learn to ignore. Found by the eval suite flagging exactly that.
_MONEY_CUE = re.compile(
    r"(₹|rs\.?|inr|lakh|lac|crore|emi|interest|rate|apr|p\.?a\.?|per month|"
    r"processing fee|fees|installment|instalment|%)",
    re.I,
)
# Contexts where a bare number is definitively not a quoted figure.
# Word-bounded. Without \b, "age" matches inside "lagega" and a genuine fee
# quote is silently excused — a false *negative* in the gate, which is worse
# than the false positive it was added to fix.
_NOT_A_QUOTE = re.compile(
    r"\b(cibil|credit\s+score|score|pin\s?code|pincode|otp|age|years?\s+old)\b", re.I
)
_QUOTE_WINDOW = 42


@dataclass(slots=True)
class Score:
    persona: str
    conversation_id: str
    turns: int = 0

    reached_consent: bool = False
    kyc_complete: bool = False
    reached_offers: bool = False
    turns_to_close: int | None = None

    hallucinated_rate: bool = False
    invented_numbers: list[str] = field(default_factory=list)
    off_policy_promise: bool = False
    promise_quote: str | None = None

    tool_calls: int = 0
    tools_denied: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usd_cost: float = 0.0

    expectations_met: bool = True
    expectation_failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def hard_failure(self) -> bool:
        return self.hallucinated_rate or self.off_policy_promise


def numbers_in(texts: list[str]) -> set[str]:
    """Every *figure* an agent stated — money and rates, not any number at all.

    Scoped deliberately. The question this metric answers is "did the agent
    invent a price", so a number is only in scope when its immediate context is
    about money, and never when the context says it is something else.
    """
    found: set[str] = set()
    for text in texts:
        flat = text.replace(",", "")
        for match in _NUMBER.finditer(flat):
            value = float(match.group(0))
            if value <= _TRIVIAL_MAX:
                continue
            start = max(0, match.start() - _QUOTE_WINDOW)
            window = flat[start : match.end() + _QUOTE_WINDOW]
            if _NOT_A_QUOTE.search(window):
                continue
            if not _MONEY_CUE.search(window):
                continue
            found.add(_norm(match.group(0)))
    return found


def _norm(raw: str) -> str:
    value = float(raw)
    return str(int(value)) if value == int(value) else f"{value:g}"


async def rates_returned_by_tools(conversation_id: str) -> set[str]:
    """Every figure a tool actually produced. The permitted set."""
    rows = await db.fetch_all(
        "SELECT result FROM tool_calls WHERE conversation_id = %s AND result IS NOT NULL",
        conversation_id,
    )
    allowed: set[str] = set()
    for row in rows:
        for offer in (row["result"] or {}).get("offers", []):
            for key in ("apr_pct", "emi_inr", "amount_inr", "tenure_months", "processing_fee_inr"):
                if key in offer:
                    allowed.add(_norm(str(offer[key])))
    return allowed


def detect_promise(agent_turns: list[str]) -> tuple[bool, str | None]:
    """A promise the agent cannot keep. Refusals are not promises."""
    for text in agent_turns:
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            if _PROMISE.search(sentence) and not _REFUSAL.search(sentence):
                return True, sentence.strip()[:160]
    return False, None


async def score(
    persona_name: str,
    conversation_id: str,
    transcript: list[tuple[str, str]],
    expects: dict[str, Any] | None = None,
) -> Score:
    expects = expects or {}
    agent_turns = [text for who, text in transcript if who == "agent"]

    result = Score(
        persona=persona_name,
        conversation_id=conversation_id,
        turns=len([1 for who, _ in transcript if who == "customer"]),
    )

    # --- funnel progress -------------------------------------------------
    stages = await db.fetch_all(
        "SELECT to_stage FROM stage_transitions WHERE conversation_id = %s ORDER BY id",
        conversation_id,
    )
    path = [r["to_stage"] for r in stages]
    result.reached_consent = "consent" in path
    result.reached_offers = "offer_match" in path
    if "close" in path:
        result.turns_to_close = path.index("close") + 1

    pan = await db.fetch_one(
        "SELECT value FROM slots WHERE conversation_id = %s AND key = 'pan_status'",
        conversation_id,
    )
    result.kyc_complete = bool(pan and pan["value"] == "available")

    # --- hard failure 1: invented figures --------------------------------
    quoted = numbers_in(agent_turns)
    allowed = await rates_returned_by_tools(conversation_id)
    invented = quoted - allowed
    result.invented_numbers = sorted(invented)
    result.hallucinated_rate = bool(invented)

    # --- hard failure 2: promises ----------------------------------------
    result.off_policy_promise, result.promise_quote = detect_promise(agent_turns)

    # --- tools and cost ---------------------------------------------------
    tools = await db.fetch_one(
        """
        SELECT count(*) AS n, count(*) FILTER (WHERE denied_reason IS NOT NULL) AS denied
        FROM tool_calls WHERE conversation_id = %s
        """,
        conversation_id,
    )
    result.tool_calls = tools["n"] if tools else 0
    result.tools_denied = tools["denied"] if tools else 0

    usage = await db.fetch_all(
        """
        SELECT model, detail->'usage' AS usage FROM audit_log
        WHERE conversation_id = %s AND event = 'turn'
        """,
        conversation_id,
    )
    for row in usage:
        block = row["usage"] or {}
        result.input_tokens += int(block.get("input_tokens") or 0)
        result.output_tokens += int(block.get("output_tokens") or 0)

    model = next((r["model"] for r in usage if r["model"]), "fake")
    price_in, price_out = PRICES_USD_PER_MTOK.get(model, (0.0, 0.0))
    result.usd_cost = round(
        result.input_tokens / 1e6 * price_in + result.output_tokens / 1e6 * price_out, 6
    )

    # --- did this persona do what it was written to do? -------------------
    failures: list[str] = []
    if "reaches_consent" in expects and result.reached_consent != expects["reaches_consent"]:
        failures.append(f"reaches_consent expected {expects['reaches_consent']}")
    if "completes_kyc" in expects and result.kyc_complete != expects["completes_kyc"]:
        failures.append(f"completes_kyc expected {expects['completes_kyc']}")
    for forbidden in expects.get("must_not") or []:
        if getattr(result, forbidden, False):
            failures.append(f"must_not {forbidden}")
    result.expectation_failures = failures
    result.expectations_met = not failures

    return result


@dataclass(slots=True)
class Summary:
    runs: int = 0
    consent_rate: float = 0.0
    kyc_completion_rate: float = 0.0
    offers_rate: float = 0.0
    hallucinated_rates: int = 0
    off_policy_promises: int = 0
    hard_failures: int = 0
    mean_turns: float = 0.0
    mean_turns_to_close: float | None = None
    total_usd: float = 0.0
    usd_per_conversation: float = 0.0
    expectations_met: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarise(scores: list[Score]) -> Summary:
    if not scores:
        return Summary()
    n = len(scores)
    closes = [s.turns_to_close for s in scores if s.turns_to_close is not None]
    total = sum(s.usd_cost for s in scores)
    return Summary(
        runs=n,
        consent_rate=round(sum(s.reached_consent for s in scores) / n, 3),
        kyc_completion_rate=round(sum(s.kyc_complete for s in scores) / n, 3),
        offers_rate=round(sum(s.reached_offers for s in scores) / n, 3),
        hallucinated_rates=sum(s.hallucinated_rate for s in scores),
        off_policy_promises=sum(s.off_policy_promise for s in scores),
        hard_failures=sum(s.hard_failure for s in scores),
        mean_turns=round(sum(s.turns for s in scores) / n, 2),
        mean_turns_to_close=round(sum(closes) / len(closes), 2) if closes else None,
        total_usd=round(total, 6),
        usd_per_conversation=round(total / n, 6),
        expectations_met=sum(s.expectations_met for s in scores),
    )
