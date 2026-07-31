"""What the nudge actually says.

The plan's line: *"You'd stopped at income details" beats "just checking in" —
and it's why you stored the stage.* Every re-entry names the drop-off point,
because a message that does not is indistinguishable from spam and gets the
number blocked.

Two modes, and the distinction is WhatsApp policy rather than a stylistic
choice:

  * **Inside the 24-hour customer-service window** — free-form. The model writes
    it, with the stage and the customer's profile in hand.
  * **Outside it** — an approved template, with variables. Not "a template-ish
    message we wrote"; a fixed string with numbered placeholders, because that
    is the only thing Meta will deliver. Bringing this up unprompted is the
    signal that you read the platform docs rather than a tutorial.
"""

from __future__ import annotations

from typing import Any

from app import db
from app.graph import prompts
from app.llm import ModelError, get_provider
from app.logging import get_logger
from app.memory import profile

log = get_logger(__name__)

# What the customer was in the middle of, in words they would recognise. Keyed
# by the stage they dropped at — which is the whole reason stage_at_drop is a
# column and not something reconstructed later.
DROP_OFF_POINT: dict[str, str] = {
    "intent_route": "telling me what kind of loan you were after",
    "qualify": "sharing your income details",
    "consent": "giving permission to check with lenders",
    "kyc_collect": "confirming your PAN",
    "offer_match": "looking at the options I found",
    "close": "finishing up",
}


# ---------------------------------------------------------------------------
# Templates — for use outside the 24h window.
#
# In production each of these is submitted to Meta and approved before it can be
# sent, which is why they are a fixed catalogue with placeholders rather than
# anything generated. Phase 11's deploy notes mention approval latency; this is
# the thing that waits on it.
# ---------------------------------------------------------------------------
TEMPLATES: dict[str, str] = {
    "resume_qualify": (
        "Hi! We were sorting out your loan options and stopped at {1}. "
        "Reply here whenever you'd like to pick it up — no rush."
    ),
    "resume_consent": (
        "Hi! Your loan enquiry is still open — we just need your go-ahead to "
        "check with our partner lenders. Reply YES to continue, or STOP to opt out."
    ),
    "resume_kyc": (
        "Hi! We were checking loan options for you and stopped at {1}. "
        "Reply here to continue, or STOP if you'd rather not hear from us."
    ),
    "resume_offers": (
        "Hi! We found some loan options for you and they're still available. "
        "Reply here to take a look, or STOP to opt out."
    ),
}

TEMPLATE_FOR_STAGE: dict[str, str] = {
    "intent_route": "resume_qualify",
    "qualify": "resume_qualify",
    "consent": "resume_consent",
    "kyc_collect": "resume_kyc",
    "offer_match": "resume_offers",
}

REENTRY_SYSTEM = """\
You are re-opening a WhatsApp conversation that went quiet. The customer has \
not replied for a while.

Rules:
- ONE short sentence, two at most. This is a nudge, not a pitch.
- Name where they left off, specifically. "Just checking in" is what spam says.
- No pressure, no urgency, no "don't miss out". They already ignored you once.
- Never quote a rate, EMI or fee. You have not called a lender for this message.
- Offer an easy exit: they can reply STOP.
- Mirror their language. If the conversation was Hinglish, write Hinglish.
"""


def template_for(stage: str) -> tuple[str, str]:
    """(template_name, rendered_text) for a conversation outside the window."""
    name = TEMPLATE_FOR_STAGE.get(stage, "resume_qualify")
    body = TEMPLATES[name].replace("{1}", DROP_OFF_POINT.get(stage, "your loan enquiry"))
    return name, body


async def compose(
    conversation_id: str, stage_at_drop: str, *, use_template: bool, attempt: int
) -> tuple[str, str | None]:
    """Returns (text, template_name). template_name is None for free-form."""
    if use_template:
        name, body = template_for(stage_at_drop)
        log.info("reentry_template", template=name, stage=stage_at_drop, attempt=attempt)
        return body, name

    slot_rows = await db.fetch_all(
        "SELECT key, value FROM slots WHERE conversation_id = %s", conversation_id
    )
    slots = {r["key"]: r["value"] for r in slot_rows}

    rows = await db.fetch_all(
        """
        SELECT direction, body FROM messages
        WHERE conversation_id = %s ORDER BY id DESC LIMIT 6
        """,
        conversation_id,
    )
    history = [
        {"role": "customer" if r["direction"] == "in" else "agent", "text": r["body"]}
        for r in reversed(rows)
    ]

    where = DROP_OFF_POINT.get(stage_at_drop, "your loan enquiry")
    profile_block = profile.render(slots, slots.get("consent"))
    user = (
        f"THEY STOPPED AT: {where}\n"
        f"THIS IS NUDGE NUMBER: {attempt + 1}\n"
        + (f"\nKNOWN ABOUT THIS CUSTOMER:\n{profile_block}\n" if profile_block else "")
        + "\nWrite the re-entry message."
    )

    try:
        result = await get_provider().reply(system=REENTRY_SYSTEM, user=user, history=history)
        if result.text.strip():
            return result.text.strip(), None
    except ModelError as exc:
        log.warning("reentry_generation_failed", error=str(exc))

    # A template is a perfectly good fallback — it is what we would have sent
    # outside the window anyway, and it still names the drop-off point.
    name, body = template_for(stage_at_drop)
    return body, name


def unused_prompt_hash() -> str:
    """Kept so Phase 07's audit log can record which prompt set was in force."""
    return prompts.prompt_hash()


__all__: list[Any] = [
    "DROP_OFF_POINT",
    "REENTRY_SYSTEM",
    "TEMPLATES",
    "compose",
    "template_for",
]
