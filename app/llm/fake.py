"""A deterministic stand-in for a real model.

This is not a mock in the "assert it was called" sense. It is a small rule-based
implementation of the same contract, good enough to drive the entire funnel end
to end, so that:

  * the test suite is free, offline, and produces the same result every run —
    a graph test that depends on a live model tests the model, not the graph;
  * `TK_LLM_PROVIDER=fake` is a usable demo mode when a key is missing or a
    rate limit is hit;
  * Phase 08's eval harness has a zero-cost baseline to compare a real model
    against.

It reads Hinglish deliberately — "PAN nahi hai", "band karo", "kitna interest" —
because that is what the labelled set in Phase 09 is made of, and a fake that
only understands English would quietly make the tests easier than reality.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from app.llm.base import Embedding, Extraction, Reply, Usage
from app.settings import get_settings

# --------------------------------------------------------------------------
# Patterns. Kept blunt on purpose: this is a fixture, not a model.
# --------------------------------------------------------------------------
_PRODUCT = [
    (re.compile(r"\b(personal|pl)\b|personal loan", re.I), "personal_loan"),
    (re.compile(r"\bhome\b|housing|ghar", re.I), "home_loan"),
    (re.compile(r"\bbusiness\b|vyapar|udyog", re.I), "business_loan"),
    (re.compile(r"\bgold\b|sona", re.I), "gold_loan"),
]

# "5 lakh", "5l", "5,00,000", "500000"
_LAKH = re.compile(r"(\d+(?:\.\d+)?)\s*(?:lakh|lac|l\b)", re.I)
_PLAIN_AMOUNT = re.compile(r"\b(\d[\d,]{4,})\b")

# "60k", "salary 60000", "monthly 45,000", "salary 1 lakh se zyada"
_INCOME_K = re.compile(r"(\d+(?:\.\d+)?)\s*k\b", re.I)
_INCOME_WORD = re.compile(
    r"(?:salary|income|kamata|kamati|kamai|monthly|mahina)\D{0,12}(\d[\d,]{3,})", re.I
)
# An income stated in lakhs. Matched — and then *removed from the text* — before
# the loan amount is parsed, because "salary 1 lakh se zyada hai" otherwise reads
# as a request for a one-lakh loan and silently overwrites the real amount.
_INCOME_LAKH = re.compile(
    r"(?:salary|income|kamata|kamati|kamai|monthly|mahina)\D{0,12}"
    r"(\d+(?:\.\d+)?)\s*(?:lakh|lac|l\b)",
    re.I,
)

# Hinglish puts words between the noun and the verb — "PAN bhi hai", "PAN toh
# nahi hai" — so these allow up to two intervening words. MISSING is tested
# first, which is what keeps "PAN nahi hai" from matching PRESENT as well.
_PAN_MISSING = re.compile(r"pan\b(?:\s+\w+){0,2}\s*(?:nahi|nhi|not|no\b|missing|nope)", re.I)
_PAN_PRESENT = re.compile(
    r"pan\b(?:\s+\w+){0,2}\s*(?:hai|yes|ready|available|h\b)|\b[A-Z]{5}\d{4}[A-Z]\b", re.I
)

_OPT_OUT = re.compile(
    r"\b(stop|unsubscribe|band\s*kar|mat\s*bhejo|do\s*not\s*(?:message|contact)|"
    r"don'?t\s*(?:message|contact)|remove\s*me|nahi\s*chahiye)\b",
    re.I,
)
_AFFIRM = re.compile(
    r"\b(yes|yeah|yep|haan|haa|ha|ok|okay|thik|theek|sahi|agree|sure|done)\b", re.I
)
_DECLINE = re.compile(r"\b(no|nope|nahi|nhi|not now|baad me|later)\b", re.I)

# "zyada" and "high" are plain quantifiers, not complaints: "salary 1 lakh se
# zyada hai" means "more than a lakh", and reading it as an objection derails a
# perfectly good qualifying answer. An objection needs either a cost noun or an
# unambiguous complaint word.
_COST_TERM = re.compile(
    r"\b(interest|rate|rates|byaj|charge|charges|fee|fees|processing|emi)\b", re.I
)
_COMPLAINT = re.compile(r"\b(costly|mehenga|mehngi|expensive|too high)\b", re.I)


def _objection_in(text: str) -> str | None:
    if m := _COST_TERM.search(text):
        return m.group(0).lower()
    if m := _COMPLAINT.search(text):
        return m.group(0).lower()
    return None


_OFF_TOPIC = re.compile(r"\b(weather|cricket|match|movie|khana|mausam|kaise ho)\b", re.I)
_HUMAN = re.compile(r"\b(human|agent|manager|representative|baat kara|complaint)\b", re.I)

_STAGE_LINE = re.compile(r"^CURRENT STAGE:\s*(\S+)", re.M)
_MESSAGE_BLOCK = re.compile(r"CUSTOMER MESSAGE:\s*\n(.*)\Z", re.S)
_STOPPED_AT = re.compile(r"^THEY STOPPED AT:\s*(.+)$", re.M)


def _income_band(rupees: int) -> str:
    if rupees < 25_000:
        return "under_25k"
    if rupees < 50_000:
        return "25k_50k"
    if rupees < 100_000:
        return "50k_1l"
    return "above_1l"


class FakeProvider:
    name = "fake"

    async def extract(self, *, system: str, user: str, schema: dict[str, Any]) -> Extraction:
        stage_match = _STAGE_LINE.search(user)
        stage = stage_match.group(1) if stage_match else "intent_route"

        body_match = _MESSAGE_BLOCK.search(user)
        text = (body_match.group(1) if body_match else user).strip()

        data: dict[str, Any] = {}

        # --- interrupts, checked before anything else --------------------
        if _OPT_OUT.search(text):
            data["opted_out"] = True
            data["interrupt"] = "opt_out"
        elif _HUMAN.search(text):
            data["interrupt"] = "escalate"
        elif objection := _objection_in(text):
            data["interrupt"] = "objection"
            data["objection"] = objection
        elif _OFF_TOPIC.search(text):
            data["interrupt"] = "off_topic"
        else:
            data["interrupt"] = None

        # --- slots --------------------------------------------------------
        for pattern, value in _PRODUCT:
            if pattern.search(text):
                data["product"] = value
                break

        # Income first, and its span is then hidden from the amount parser.
        # A number can be an income or a loan amount but never both, and the
        # keyword is the only thing that disambiguates them.
        amount_text = text
        if lakh_income := _INCOME_LAKH.search(text):
            data["income_band"] = _income_band(int(float(lakh_income.group(1)) * 100_000))
            amount_text = text[: lakh_income.start()] + " " + text[lakh_income.end() :]
        elif word := _INCOME_WORD.search(text):
            data["income_band"] = _income_band(int(word.group(1).replace(",", "")))
            amount_text = text[: word.start()] + " " + text[word.end() :]
        elif k := _INCOME_K.search(text):
            data["income_band"] = _income_band(int(float(k.group(1)) * 1000))
            amount_text = text[: k.start()] + " " + text[k.end() :]

        if lakh := _LAKH.search(amount_text):
            data["amount_inr"] = int(float(lakh.group(1)) * 100_000)
        elif plain := _PLAIN_AMOUNT.search(amount_text):
            amount = int(plain.group(1).replace(",", ""))
            if amount >= 10_000:
                data["amount_inr"] = amount

        if _PAN_MISSING.search(text):
            data["pan_status"] = "missing"
        elif _PAN_PRESENT.search(text):
            data["pan_status"] = "available"

        # --- consent is only meaningful where it was asked ----------------
        if stage == "consent":
            if _OPT_OUT.search(text) or _DECLINE.search(text):
                data["consent_granted"] = False
            elif _AFFIRM.search(text):
                data["consent_granted"] = True

        # Only keep keys the schema actually declares, so the fake cannot
        # invent a slot the real provider would never return.
        allowed = set((schema.get("properties") or {}).keys())
        data = {k: v for k, v in data.items() if k in allowed}

        return Extraction(data=data, usage=Usage(input_tokens=len(user) // 4, calls=1))

    async def reply(self, *, system: str, user: str, history: list[dict[str, str]]) -> Reply:
        # Re-entry prompts have no CURRENT STAGE line — they say where the
        # customer stopped. Without this branch the fake answered every nudge
        # with its generic greeting, which silently made the scheduler tests
        # assert nothing.
        if stopped := _STOPPED_AT.search(user):
            text = (
                f"Hi! We'd got as far as {stopped.group(1).strip()} — "
                "happy to pick up whenever you are. Reply STOP if you'd rather not."
            )
            return Reply(
                text=text, usage=Usage(input_tokens=len(user) // 4, output_tokens=28, calls=1)
            )

        stage_match = _STAGE_LINE.search(user)
        stage = stage_match.group(1) if stage_match else "intent_route"
        text = _CANNED.get(stage, _CANNED["intent_route"])
        return Reply(text=text, usage=Usage(input_tokens=len(user) // 4, output_tokens=24, calls=1))

    async def embed(self, *, text: str) -> Embedding:
        """Hashing-trick bag-of-words, L2-normalised.

        Not semantic — it has no idea "fees" and "charges" are related. But it
        is genuinely *lexical*: texts sharing words get higher cosine similarity,
        which is enough to exercise retrieval mechanics offline and deterministically.
        Anything measuring whether retrieval actually helps has to run against a
        real embedding model, and `evals/memory_ab.py` says so.
        """
        dims = get_settings().embedding_dimensions
        vector = [0.0] * dims
        for word in re.findall(r"\w+", text.lower(), re.UNICODE):
            bucket = int(hashlib.sha256(word.encode("utf-8")).hexdigest()[:8], 16) % dims
            vector[bucket] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return Embedding(vector=[v / norm for v in vector], usage=Usage(calls=1))


_CANNED = {
    "intent_route": (
        "Hi! I can help you compare loan options. "
        "What kind of loan are you looking for — personal, home, or business?"
    ),
    "qualify": (
        "Got it. To find the right options, could you tell me your monthly "
        "income and which city you're in?"
    ),
    "consent": (
        "Before I check offers, I need your permission to share your details "
        "with our partner lenders. Reply YES to continue."
    ),
    "kyc_collect": ("Thanks! For the lender check I'll need your PAN. Do you have it handy?"),
    "offer_match": ("Perfect — let me pull up the options you qualify for. One moment."),
    "close": "Thanks for your time. I've noted this and won't message further.",
    "escalate": ("Let me get a colleague to help with this — someone will pick this up shortly."),
    "handle_objection": (
        "That's a fair question. Rates depend on the lender and your profile, "
        "and I'll only ever quote you what a lender actually returns. "
        "Shall we carry on?"
    ),
    "handle_off_topic": ("Happy to chat, but I'm best at loans! Shall we get back to it?"),
}
