"""The consent ledger.

Under the DPDP Act consent must be specific, informed and revocable, and you
should be able to prove what was shown and when. Phase 03 kept consent as a slot
— adequate for routing, useless as evidence. This is the evidence.

Three properties:

  * **The exact wording is stored**, not only its hash. A hash proves nothing was
    altered; it cannot be read back to a human in a dispute, and "we can show you
    the SHA-256" is not an answer a regulator accepts.
  * **It is append-only**, enforced by a Postgres trigger rather than by
    convention. Revocation is a new row, not an UPDATE, so the grant remains
    visible forever — which is the point: you have to be able to show both that
    they agreed and that they later withdrew.
  * **Revocation halts everything within one turn.** Not "on the next scheduler
    pass" — the pending nudge is cancelled in the same call.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json

from app import db
from app.logging import get_logger
from app.privacy import audit

log = get_logger(__name__)

# What granting consent actually permits. Stored per event, because a scope that
# widens later is a new consent, not the same one.
DEFAULT_SCOPE = ["loan_type", "income_band", "city", "pan_status"]


async def record(
    conversation_id: str,
    customer_ref: str,
    channel: str,
    *,
    event: str,
    wording: str,
    wording_hash: str,
    source: str = "customer_reply",
    scope: list[str] | None = None,
) -> int:
    """Append one consent event. There is no update path, by design."""
    row = await db.fetch_one(
        """
        INSERT INTO consent_ledger
          (conversation_id, customer_ref, channel, event, wording, wording_hash, scope, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        conversation_id,
        customer_ref,
        channel,
        event,
        wording,
        wording_hash,
        Json(scope if scope is not None else DEFAULT_SCOPE),
        source,
    )
    await audit.write(
        conversation_id,
        "consent",
        detail={"event": event, "wording_hash": wording_hash, "source": source},
    )
    # NB: not `event=` — structlog reserves that key for the message itself.
    log.info("consent_recorded", consent_event=event, wording_hash=wording_hash, source=source)
    return row["id"]


async def current(conversation_id: str) -> dict[str, Any] | None:
    """The latest event. Revocation beats an earlier grant because it is later."""
    row = await db.fetch_one(
        """
        SELECT event, wording, wording_hash, scope, source, at
        FROM consent_ledger WHERE conversation_id = %s
        ORDER BY id DESC LIMIT 1
        """,
        conversation_id,
    )
    if row is None:
        return None
    return {
        "event": row["event"],
        "granted": row["event"] == "granted",
        "wording": row["wording"],
        "wording_hash": row["wording_hash"],
        "scope": row["scope"],
        "source": row["source"],
        "at": row["at"].isoformat(),
    }


async def is_granted(conversation_id: str) -> bool:
    state = await current(conversation_id)
    return bool(state and state["granted"])


async def history(conversation_id: str) -> list[dict[str, Any]]:
    """Every event, oldest first. This is what a dispute is settled with."""
    rows = await db.fetch_all(
        """
        SELECT event, wording, wording_hash, scope, source, at
        FROM consent_ledger WHERE conversation_id = %s ORDER BY id
        """,
        conversation_id,
    )
    return [
        {
            "event": r["event"],
            "wording": r["wording"],
            "wording_hash": r["wording_hash"],
            "scope": r["scope"],
            "source": r["source"],
            "at": r["at"].isoformat(),
        }
        for r in rows
    ]


async def revoke(conversation_id: str, *, source: str = "customer_reply") -> dict[str, Any]:
    """Withdraw consent and stop everything, in this call.

    "Revocable" is only true if withdrawal takes effect immediately. A system
    that records the revocation and sends one more scheduled nudge before
    noticing has not honoured it.
    """
    from app.scheduler import queue

    previous = await current(conversation_id)
    conversation = await db.fetch_one(
        "SELECT customer_ref, channel FROM conversations WHERE id = %s", conversation_id
    )
    if conversation is None:
        return {"revoked": False, "reason": "conversation_missing"}

    await record(
        conversation_id,
        conversation["customer_ref"],
        conversation["channel"],
        event="revoked",
        # The wording of the *grant* being withdrawn, so the record says what
        # was given up rather than merely that something was.
        wording=(previous or {}).get("wording", "(no prior grant on record)"),
        wording_hash=(previous or {}).get("wording_hash", ""),
        source=source,
        scope=[],
    )

    cancelled = await queue.cancel(conversation_id, "consent_revoked")
    await db.execute("UPDATE conversations SET status = 'opted_out' WHERE id = %s", conversation_id)
    await db.execute(
        """
        INSERT INTO slots (conversation_id, key, value, source)
        VALUES (%s, 'opted_out', 'true'::jsonb, 'confirmed')
        ON CONFLICT (conversation_id, key)
          DO UPDATE SET value = 'true'::jsonb, source = 'confirmed', updated_at = now()
        """,
        conversation_id,
    )

    log.info("consent_revoked", followups_cancelled=cancelled)
    return {"revoked": True, "followups_cancelled": cancelled}
