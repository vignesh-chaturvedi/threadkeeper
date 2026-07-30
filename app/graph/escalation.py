"""The escalation packet.

Built straight from the product description: when a conversation goes to a
human, that human should not have to read forty messages to work out what is
happening. They get the transcript, the stage it stalled at, everything the
system believes about the customer, why it escalated, and the last offer shown.

Built at the moment of escalation rather than when someone opens the queue,
because the context that caused it is cheapest to capture while it is still in
hand — and because a packet assembled later would reflect a conversation that
has since moved on.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json

from app import db
from app.logging import get_logger

log = get_logger(__name__)

TRANSCRIPT_LIMIT = 40


async def build_packet(conversation_id: str, reason: str, state: dict[str, Any]) -> dict[str, Any]:
    conversation = await db.fetch_one(
        """
        SELECT id, channel, customer_ref, stage, status, last_in_at, created_at
        FROM conversations WHERE id = %s
        """,
        conversation_id,
    )
    messages = await db.fetch_all(
        """
        SELECT direction, body, received_at FROM messages
        WHERE conversation_id = %s ORDER BY id DESC LIMIT %s
        """,
        conversation_id,
        TRANSCRIPT_LIMIT,
    )
    transitions = await db.fetch_all(
        """
        SELECT from_stage, to_stage, reason, at FROM stage_transitions
        WHERE conversation_id = %s ORDER BY id
        """,
        conversation_id,
    )

    slots = state.get("slots") or {}
    return {
        "conversation": {
            "id": str(conversation["id"]) if conversation else conversation_id,
            "channel": conversation["channel"] if conversation else None,
            # The token, never the phone number — a human console is one more
            # place a customer's digits would otherwise end up.
            "customer_ref": conversation["customer_ref"] if conversation else None,
            "opened_at": conversation["created_at"].isoformat() if conversation else None,
        },
        "stage": state.get("stage"),
        "reason": reason,
        "intent": {
            "product": slots.get("product"),
            "amount_inr": slots.get("amount_inr"),
            "objection": slots.get("objection"),
        },
        "slots": slots,
        "consent": state.get("consent") or {},
        # Phase 05 fills this once the lender tools exist. Present and empty is
        # more useful to a human than absent.
        "last_offer_shown": state.get("last_offer"),
        "funnel_path": [
            {
                "from": t["from_stage"],
                "to": t["to_stage"],
                "reason": t["reason"],
                "at": t["at"].isoformat(),
            }
            for t in transitions
        ],
        "transcript": [
            {
                "who": "customer" if m["direction"] == "in" else "agent",
                "text": m["body"],
                "at": m["received_at"].isoformat(),
            }
            for m in reversed(messages)
        ],
    }


async def record(
    conversation_id: str, stage: str, reason: str, state: dict[str, Any]
) -> int | None:
    packet = await build_packet(conversation_id, reason, state)
    row = await db.fetch_one(
        """
        INSERT INTO escalations (conversation_id, stage_at_escalation, reason, packet)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        conversation_id,
        stage,
        reason,
        Json(packet),
    )
    log.info("escalation_recorded", stage=stage, reason=reason, turns=len(packet["transcript"]))
    return row["id"] if row else None


async def open_queue(limit: int = 50) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        """
        SELECT id, conversation_id, stage_at_escalation, reason, packet, created_at
        FROM escalations WHERE resolved_at IS NULL
        ORDER BY created_at DESC LIMIT %s
        """,
        limit,
    )
    return [
        {
            "id": r["id"],
            "conversation_id": str(r["conversation_id"]),
            "stage": r["stage_at_escalation"],
            "reason": r["reason"],
            "created_at": r["created_at"].isoformat(),
            "packet": r["packet"],
        }
        for r in rows
    ]
