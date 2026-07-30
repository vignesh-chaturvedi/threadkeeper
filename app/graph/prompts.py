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

# ---------------------------------------------------------------------------
# Extraction — structured output, temperature 0, no prose
# ---------------------------------------------------------------------------
EXTRACTION_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "product": {
            "type": "STRING",
            "enum": ["personal_loan", "home_loan", "business_loan", "gold_loan"],
            "description": "Only if the customer named a loan type. Do not guess.",
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
            "description": (
                "Only set when the previous agent turn asked for consent to share "
                "details with lenders, and only from an unambiguous yes or no."
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
    },
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
the reply is unambiguous. "ok" answering a different question is not consent.
"""

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
- NEVER quote an interest rate, EMI, fee or approval amount. You have not \
called a lender. Say you'll check, and that figures come from the lender.
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
        "Tell them you're checking which options they qualify for. Do not invent "
        "any option, rate or amount — none have been fetched yet."
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
