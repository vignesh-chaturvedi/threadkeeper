"""The six tools, defined once.

Tool design is the actual skill this phase is about, so the shapes are narrow on
purpose:

  * Every argument is one the agent can actually know. No tool takes a free-form
    "query" string, because a tool that accepts anything can be talked into
    anything.
  * Every result is a flat dict with a fixed key set, including on failure.
    A tool that raises on a lender timeout makes every caller responsible for
    the lender's uptime; one that returns `{"error": ..., "retryable": true}`
    keeps that decision in one place.
  * `create_application` cannot be called without a `consent_ref` and an
    `idem_key`. Not "should not" — the signature will not permit it.

These are plain async functions. `app/tools/server.py` exposes them over MCP and
`app/tools/client.py` calls them in-process; both go through the same guard and
the same audit trail, so there is exactly one implementation to reason about.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app import db
from app.logging import get_logger
from app.tools import lender
from app.tools.lender import LenderTimeout, LenderUnavailable

log = get_logger(__name__)

# Argument names whose values must never reach the audit log in the clear.
# Phase 07 replaces this with the tokenizer; until then it is a blunt mask,
# which is still better than a PAN sitting in a jsonb column forever.
SENSITIVE_ARGS: frozenset[str] = frozenset({"pan", "aadhaar", "account_number"})


def mask(arguments: dict[str, Any]) -> dict[str, Any]:
    return {k: ("***redacted***" if k in SENSITIVE_ARGS and v else v) for k, v in arguments.items()}


def _fail(exc: Exception) -> dict[str, Any]:
    """One failure shape for every tool.

    Retryable is the caller's most important question and the one a bare
    exception does not answer.
    """
    if isinstance(exc, LenderTimeout):
        return {"error": "lender_timeout", "retryable": True, "detail": str(exc)}
    if isinstance(exc, LenderUnavailable):
        return {"error": "lender_unavailable", "retryable": True, "detail": str(exc)}
    return {"error": "tool_failed", "retryable": False, "detail": str(exc)}


# ---------------------------------------------------------------------------
# read tools
# ---------------------------------------------------------------------------
async def check_eligibility(
    *, product: str, income_band: str, city_tier: int = 2, amount_inr: int = 300_000, **_: Any
) -> dict[str, Any]:
    """Which lenders would consider this customer, and why the others would not."""
    try:
        result = await lender.check_eligibility(product, income_band, city_tier, amount_inr)
    except Exception as exc:  # noqa: BLE001 — normalised into a result shape
        return _fail(exc)
    return {
        "eligible": result.eligible,
        "lender_count": len(result.lenders),
        "refusals": result.refusals,
    }


async def fetch_offers(
    *,
    product: str,
    income_band: str,
    city_tier: int = 2,
    amount_inr: int = 300_000,
    conversation_id: str | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Ranked offers for a qualified lead. Never invents a rate."""
    try:
        offers = await lender.match(product, income_band, city_tier, amount_inr)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)

    return {
        "offers": offers,
        "quoted_at": datetime.now(UTC).isoformat(),
        # Every figure here is indicative until a lender underwrites it, and the
        # agent is required to say so.
        "indicative": True,
    }


async def verify_pan(*, pan: str, conversation_id: str | None = None, **_: Any) -> dict[str, Any]:
    """Structural check only. Never returns or records the number.

    One of exactly two callers permitted to detokenize: the lender genuinely
    needs the digits. Everything else in the system works on the token.
    """
    from app.privacy import tokenize as _tokenize

    real = await _tokenize.detokenize(pan, conversation_id) if _tokenize.has_tokens(pan) else pan
    try:
        return await lender.verify_pan(real)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)


# ---------------------------------------------------------------------------
# write tools
# ---------------------------------------------------------------------------
async def create_application(
    *,
    conversation_id: str,
    offer_id: str,
    consent_ref: str,
    idem_key: str,
    **_: Any,
) -> dict[str, Any]:
    """Open a loan application. Requires a consent_ref — enforced here, not in the prompt.

    The guard already refused this without consent. Checking again is not
    redundancy for its own sake: the guard knows the graph's state, this knows
    the database, and the one that matters legally is the database.
    """
    consent = await db.fetch_one(
        "SELECT value FROM slots WHERE conversation_id = %s AND key = 'consent'",
        conversation_id,
    )
    granted = bool(consent and (consent["value"] or {}).get("granted"))
    if not granted:
        return {"error": "consent_missing", "retryable": False}

    expected_ref = (consent["value"] or {}).get("wording_hash")
    if expected_ref and consent_ref != expected_ref:
        # The consent on file is for different wording than the one being cited.
        return {"error": "consent_ref_mismatch", "retryable": False}

    offer = await _quoted_offer(conversation_id, offer_id)
    if offer is None:
        # An offer id that was never shown to this customer is, by definition,
        # one the model made up.
        return {"error": "offer_not_quoted", "retryable": False}

    try:
        result = await lender.apply(offer, idem_key)
    except Exception as exc:  # noqa: BLE001
        return _fail(exc)

    await db.execute(
        """
        INSERT INTO applications
          (id, conversation_id, offer_id, lender, consent_ref, amount_inr, apr_pct)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
        """,
        result["application_id"],
        conversation_id,
        offer_id,
        offer["lender"],
        consent_ref,
        offer.get("amount_inr"),
        offer.get("apr_pct"),
    )
    log.info("application_created", application_id=result["application_id"], lender=offer["lender"])
    return result


async def schedule_followup(
    *,
    conversation_id: str,
    delay_hours: float = 24.0,
    reason: str = "no_reply",
    stage_at_drop: str = "unknown",
    idem_key: str,
    **_: Any,
) -> dict[str, Any]:
    """Defer this conversation. Phase 06 owns the worker that acts on it."""
    due_at = datetime.now(UTC) + timedelta(hours=delay_hours)
    row = await db.fetch_one(
        """
        INSERT INTO followups (conversation_id, due_at, reason, stage_at_drop)
        VALUES (%s, %s, %s, %s)
        RETURNING id, due_at
        """,
        conversation_id,
        due_at,
        reason,
        stage_at_drop,
    )
    return {"followup_id": row["id"], "due_at": row["due_at"].isoformat(), "reason": reason}


async def escalate_to_human(
    *, conversation_id: str, reason: str = "agent_requested", **_: Any
) -> dict[str, Any]:
    """Hand off. Allowed from any stage, for the same reason opt-out is."""
    from app.graph import escalation

    state = await _state_for(conversation_id)
    packet_id = await escalation.record(
        conversation_id, state.get("stage", "unknown"), reason, state
    )
    await db.execute("UPDATE conversations SET status = 'escalated' WHERE id = %s", conversation_id)
    return {"escalation_id": packet_id, "reason": reason, "status": "queued_for_human"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
async def _quoted_offer(conversation_id: str, offer_id: str) -> dict[str, Any] | None:
    """Find an offer this conversation was actually shown.

    Reading it back out of the audit log rather than trusting the argument is
    what turns "the agent must not invent an offer" into something enforced.
    """
    rows = await db.fetch_all(
        """
        SELECT result FROM tool_calls
        WHERE conversation_id = %s AND tool = 'fetch_offers' AND result IS NOT NULL
        ORDER BY id DESC LIMIT 5
        """,
        conversation_id,
    )
    for row in rows:
        for offer in (row["result"] or {}).get("offers", []):
            if offer.get("offer_id") == offer_id:
                return offer
    return None


async def _state_for(conversation_id: str) -> dict[str, Any]:
    conversation = await db.fetch_one(
        "SELECT stage FROM conversations WHERE id = %s", conversation_id
    )
    slot_rows = await db.fetch_all(
        "SELECT key, value FROM slots WHERE conversation_id = %s", conversation_id
    )
    slots = {r["key"]: r["value"] for r in slot_rows}
    return {
        "stage": conversation["stage"] if conversation else "unknown",
        "slots": slots,
        "consent": slots.get("consent") or {},
    }


# ---------------------------------------------------------------------------
# the registry the server and the client both read
# ---------------------------------------------------------------------------
TOOLS: dict[str, Any] = {
    "check_eligibility": check_eligibility,
    "fetch_offers": fetch_offers,
    "verify_pan": verify_pan,
    "create_application": create_application,
    "schedule_followup": schedule_followup,
    "escalate_to_human": escalate_to_human,
}

DESCRIPTIONS: dict[str, str] = {
    "check_eligibility": "Which lenders would consider this customer, and why the rest would not.",
    "fetch_offers": "Ranked, indicative offers for a qualified lead. Figures come from the lender.",
    "verify_pan": "Structurally validate a PAN. Never returns or stores the number.",
    "create_application": (
        "Open a loan application. Requires granted consent and an idempotency key."
    ),
    "schedule_followup": "Schedule a re-entry attempt for a conversation that has gone quiet.",
    "escalate_to_human": "Hand the conversation to a person, with a context packet.",
}
