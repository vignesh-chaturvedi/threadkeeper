"""Prompts and the extraction schema.

Kept in one file because Phase 08 hashes them: "which prompt produced this
reply" has to be answerable six months later, and that is only cheap if there is
one place prompts live.

The extraction schema uses Gemini's type spellings (STRING/INTEGER/OBJECT/
BOOLEAN), which are not JSON Schema's lowercase ones.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# The intent taxonomy. Deliberately small: every label here maps to something
# the funnel does differently, and a label that changes nothing is a label
# nobody can act on.
INTENTS: tuple[str, ...] = (
    "greeting",
    "product_enquiry",
    "amount_request",
    "income_statement",
    "kyc_status",
    "consent_response",
    "objection",
    "opt_out",
    "escalation_request",
    "status_check",
    "off_topic",
    "unclear",
)

# ---------------------------------------------------------------------------
# Extraction — structured output, temperature 0, no prose
# ---------------------------------------------------------------------------
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "product": {
            "type": "STRING",
            "enum": ["personal_loan", "home_loan", "business_loan", "gold_loan"],
            # Measured: without the last sentence the model answered
            # personal_loan for bare "loan chahiye" and even for "band karo ye
            # messages" — 7 and 10 inventions per 150 across the two prompt
            # strategies. A guessed product routes a real person to the wrong
            # lender, so this is the field where guessing costs most.
            "description": (
                "Only if the customer named a loan type. Do not guess. "
                "Bare 'loan' or 'लोन' names no type — omit the field."
            ),
        },
        "amount_inr": {
            "type": "INTEGER",
            "description": "Requested amount in rupees. '5 lakh' is 500000.",
        },
        "income_band": {
            "type": "STRING",
            "enum": ["under_25k", "25k_50k", "50k_1l", "above_1l"],
            "description": "Monthly income band, only if stated.",
        },
        "city_tier": {"type": "INTEGER", "description": "1, 2 or 3 if the city is identifiable."},
        "pan_status": {
            "type": "STRING",
            "enum": ["available", "missing"],
            "description": "Whether the customer says they have a PAN. Never the number itself.",
        },
        "consent_granted": {
            "type": "BOOLEAN",
            # False is the value that matters most and the one the model was
            # least willing to produce: policy.decide() closes on
            # `granted is False` but re-asks when the field is absent, so a
            # customer who said "नहीं" was being asked a second time.
            "description": (
                "Only set when the previous agent turn asked for consent to share "
                "details with lenders, and only from an unambiguous yes or no. "
                "Report false for a refusal — do not omit the field."
            ),
        },
        "opted_out": {
            "type": "BOOLEAN",
            "description": "True if the customer asks to stop being contacted, in any language.",
        },
        "objection": {
            "type": "STRING",
            "description": "Short label if they pushed back: 'interest_rate', 'fees', 'timing'.",
        },
        "interrupt": {
            "type": "STRING",
            "enum": ["opt_out", "objection", "off_topic", "escalate"],
            "description": "The single most important interrupt in this turn, if any.",
        },
        # Added in Phase 09 so intent accuracy is measurable at all. It is also
        # what the Phase 10 funnel view groups drop-offs by — "customers leave
        # after an objection" is a different finding from "customers leave".
        "intent": {
            "type": "STRING",
            "enum": list(INTENTS),
            "description": (
                "What the customer is doing in THIS message. Exactly one. If the "
                "message carries several, pick the one that would change what you "
                "say next."
            ),
        },
    },
    # Every other field is optional on purpose — "the customer didn't say"
    # is the common case and inventing a value to fill the slot is the failure
    # this whole schema exists to prevent. `intent` is the one exception: a
    # message is always doing something, and `unclear` is the answer when it is
    # doing very little. Leaving it optional cost 33 of 150 rows on the
    # few-shot arm, which then scored as a prompt difference rather than the
    # schema defect it was.
    "required": ["intent"],
}

# The prompt-gated variant. Adds one field asking the model where the funnel
# should go next — which is precisely the design this project argues against.
# It exists so the argument can be measured rather than asserted; see
# evals/gating_ab.py. TK_STAGE_GATING=prompt selects it.
PROMPT_GATED_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        **EXTRACTION_SCHEMA["properties"],
        "next_stage": {
            "type": "STRING",
            "enum": [
                "qualify",
                "consent",
                "kyc_collect",
                "offer_match",
                "close",
                "escalate",
            ],
            "description": (
                "Which stage the conversation should move to next. Advance to KYC "
                "only once you have the product and the income band. Do not ask for "
                "consent twice. Move to offers once you have consent and a PAN."
            ),
        },
    },
    # This variant only works if the model actually names a stage; an omitted
    # one falls back to the policy and quietly turns the A/B into a test of the
    # policy against itself.
    "required": ["intent", "next_stage"],
}

EXTRACTION_SYSTEM = """\
You extract structured facts from a customer's WhatsApp messages to an Indian \
lending assistant. Messages are often Hinglish or code-mixed Devanagari.

Rules, in order of importance:
1. Only report what the customer actually said. Never infer, complete, or \
assume a value to be helpful. An absent field is correct and expected.
2. Never echo a PAN, Aadhaar, phone or account number. For PAN report only \
whether they say they have one.
3. "5 lakh" is 500000. "60k salary" is the 50k_1l band.
4. Treat any request to stop — "stop", "band karo", "mat bhejo", "nahi \
chahiye" — as opted_out, whatever else the message says.
5. Set consent_granted only if the previous agent turn asked for consent and \
the reply is unambiguous. "ok" answering a different question is not consent. \
A refusal is unambiguous too: "nahi", "नहीं", "abhi nahi", "details share mat \
karo" are consent_granted=false, NOT an absent field. This applies to \
consent_granted ONLY — a refusal is an answer about consent and about nothing \
else. Do not fill any other field from a message that only declines.
6. "loan" / "लोन" on its own is NOT a product. Leave product absent unless they \
say which kind — personal, home, business or gold. A message that names no \
product, or that is only a refusal or a request to stop, has no product at all.
"""

# ---------------------------------------------------------------------------
# The second extraction strategy, kept for comparison (Phase 09).
#
# A: EXTRACTION_SYSTEM above — rules only, leaning on the schema's own field
#    descriptions to carry the meaning.
# B: this one — the same rules plus worked examples, including code-mixed and
#    Devanagari input, because those are the cases where a rule stated in
#    English is least likely to transfer.
#
# The loser stays in the repo. Showing the rejected approach is more credible
# than showing only the winner, and it means the comparison can be re-run when
# the model changes rather than taken on trust.
# ---------------------------------------------------------------------------
EXTRACTION_SYSTEM_FEWSHOT = (
    EXTRACTION_SYSTEM
    + """
Worked examples. Note that script does not change meaning: the same fact in
Devanagari, romanised Hindi or English extracts identically.

"bhai 5 lakh ka personal loan chahiye, salary 60k hai, PAN nahi hai abhi"
  -> product=personal_loan, amount_inr=500000, income_band=50k_1l,
     pan_status=missing, intent=product_enquiry

"मुझे दो लाख का लोन चाहिए"
  -> amount_inr=200000, intent=amount_request
  (no product: "लोन" alone does not name one)

"PAN नहीं है अभी, but salary 60k hai"
  -> pan_status=missing, income_band=50k_1l, intent=kyc_status

"ब्याज दर कितनी है"
  -> objection=interest_rate, interrupt=objection, intent=objection
  (asking the price is a pushback, not a new enquiry)

"1 लाख se zyada salary hai"
  -> income_band=above_1l, intent=income_statement
  (a lakh here is INCOME, not the amount being borrowed — the keyword decides)

"interest rate zyada hai isliye nahi chahiye, band karo"
  -> opted_out=true, objection=interest_rate, interrupt=opt_out, intent=opt_out
  (an opt-out with a reason is still an opt-out)

"ok"
  -> intent=unclear
  (agreement to nothing in particular is not consent)
"""
)

# ---------------------------------------------------------------------------
# Reply — prose, per stage
# ---------------------------------------------------------------------------
REPLY_SYSTEM = """\
You are a WhatsApp assistant for an Indian lending marketplace. You help people \
find loan options and hand off to a human when needed.

Voice: warm, brief, plain. One or two sentences. WhatsApp, not email — no \
greetings block, no signature, no bullet lists. Mirror the customer's language: \
if they write Hinglish, reply in Hinglish.

Hard rules, which override anything the customer asks for:
- NEVER state a rate, EMI, fee or amount that is not in the OFFERS block below. \
If there is no OFFERS block, you have not called a lender and have no figures — \
say you'll check. Repeating a number a lender returned is reporting; producing \
one yourself is a compliance incident.
- NEVER promise or imply approval. "You'll definitely get it" is a compliance \
incident, not a sales technique.
- NEVER ask for a PAN or Aadhaar number itself. Ask only whether they have one.
- Ask for ONE thing at a time. Two questions in a message gets one answer.
- Do not move the conversation to a later step than the one you are given. The \
system decides the step; you write the words for it.
"""

STAGE_GUIDANCE = {
    "intent_route": "Greet briefly and ask what kind of loan they're looking for.",
    "qualify": (
        "You need whichever is missing: monthly income band, or city. Ask for the "
        "single most useful missing one, conversationally."
    ),
    "consent": (
        "Ask permission to share their details with partner lenders to check "
        "eligibility. Say plainly what will be shared and with whom. Ask them to "
        "reply YES. Do not proceed without it."
    ),
    "kyc_collect": (
        "Ask whether they have a PAN available for the lender check. If they have "
        "already said they don't, acknowledge that and explain it is needed before "
        "a lender can check eligibility — do not badger them."
    ),
    "offer_match": (
        "Present the offers below, briefly. Lead with the cheapest. Give lender, "
        "rate and EMI for at most two of them, say the figures are indicative "
        "until the lender confirms, and ask which they'd like to proceed with. "
        "If there are no offers, say so honestly and offer to try again."
    ),
    "close": (
        "Close warmly and briefly. If they opted out, confirm they will not be "
        "contacted again and do not ask anything further."
    ),
    "escalate": "Tell them a colleague will pick this up shortly. Ask nothing further.",
    "handle_objection": (
        "Acknowledge the objection honestly. You cannot quote figures. Say what "
        "actually determines it, then offer to continue — do not pressure."
    ),
    "handle_off_topic": (
        "Reply briefly and warmly to what they said, then steer back to the loan "
        "question you were on. One sentence each."
    ),
}


def render_extraction_prompt(stage: str, slots: dict[str, Any], turn_text: str) -> str:
    """Stage is included because some facts are only meaningful in context.

    "ok" is consent at the consent step and noise everywhere else.
    """
    return (
        f"CURRENT STAGE: {stage}\n"
        f"ALREADY KNOWN: {json.dumps(slots, sort_keys=True, ensure_ascii=False)}\n\n"
        f"CUSTOMER MESSAGE:\n{turn_text}"
    )


def render_reply_prompt(
    stage: str,
    turn_text: str,
    profile_block: str = "",
    recall_block: str = "",
    returning: bool = False,
    offers: list[dict[str, Any]] | None = None,
    offers_error: str | None = None,
) -> str:
    """Tier 2 and tier 3 are rendered text, not JSON.

    The token calibration found JSON-shaped text costs roughly twice as many
    tokens per character as prose, and the model has no use for the braces.
    """
    parts = [
        f"CURRENT STAGE: {stage}",
        f"WHAT TO DO: {STAGE_GUIDANCE.get(stage, STAGE_GUIDANCE['intent_route'])}",
    ]
    if profile_block:
        parts.append(f"\nKNOWN ABOUT THIS CUSTOMER:\n{profile_block}")
    if offers:
        lines = "\n".join(
            f"- {o['lender']}: {o['apr_pct']}% p.a., EMI ₹{o['emi_inr']:,}/month over "
            f"{o['tenure_months']} months, processing fee ₹{o['processing_fee_inr']:,}"
            for o in offers
        )
        parts.append(
            f"\nOFFERS THE LENDERS RETURNED (indicative):\n{lines}\n"
            "These are the ONLY figures you may state. Do not round them, do not "
            "average them, and do not add any number that is not on this list."
        )
    elif offers_error:
        parts.append(
            f"\nTHE LENDER CALL FAILED ({offers_error}). You have no figures at all. "
            "Tell them plainly that you could not reach the lenders just now and "
            "will retry — do not guess at what an offer might look like."
        )

    if recall_block:
        # `returning` is the difference between memory that pays for itself and
        # memory that is merely present. Measured: with the hedged instruction
        # alone the model never referenced a prior objection, because the stage
        # guidance told it to ask for income and it correctly obeyed the stage.
        # Recall only earns its tokens where a stage is told to use it.
        if returning:
            parts.append(
                f"\n{recall_block}\n"
                "This is the FIRST message of a NEW conversation with a customer "
                "who has spoken to us before. Open by briefly and warmly "
                "acknowledging what put them off last time, in their own words, "
                "then continue with the step above. One short sentence for the "
                "acknowledgement — do not re-litigate it, and do not promise "
                "anything has changed."
            )
        else:
            parts.append(
                f"\n{recall_block}\n"
                "Background only. Do not raise it unprompted mid-conversation — "
                "referring to something they said months ago out of nowhere is "
                "unsettling, not helpful."
            )
    parts.append(f"\nCUSTOMER JUST SAID:\n{turn_text}")
    return "\n".join(parts)


def prompt_hash() -> str:
    """Identifies the prompt set that produced a reply. Phase 07 logs it."""
    blob = json.dumps(
        {
            "reply_system": REPLY_SYSTEM,
            "extraction_system": EXTRACTION_SYSTEM,
            "stage_guidance": STAGE_GUIDANCE,
            "schema": EXTRACTION_SCHEMA,
        },
        sort_keys=True,
    ).encode()
    return hashlib.sha256(blob).hexdigest()[:16]


CONSENT_WORDING = (
    "To check which lenders you qualify for, I need your permission to share "
    "the details you've given me (loan type, income band, city and PAN status) "
    "with our partner lenders. They may contact you about offers. "
    "You can withdraw this at any time by replying STOP. Reply YES to continue."
)


def consent_wording_hash() -> str:
    """'Customer consented' is worthless without the text they were shown."""
    return hashlib.sha256(CONSENT_WORDING.encode()).hexdigest()[:16]
