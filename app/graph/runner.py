"""Run one turn through the graph, and record what it did.

The checkpointer already persists state. This module exists for the things the
checkpointer cannot give you:

  * `slots` as ordinary rows, so "how many leads reached KYC without a PAN" is a
    SQL query rather than a script that deserialises checkpoints;
  * `stage_transitions` as an append-only log with the *reason* each move
    happened, which is what the Phase 10 funnel chart is built from and what
    makes the conversation auditable months later;
  * an escalation packet at the moment escalation is decided, while the context
    that caused it is still to hand.

Duplicating state into tables is a deliberate trade: the checkpoint is the
source of truth for resuming, the tables are the source of truth for asking
questions about the business.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json

from app import db
from app.graph import escalation
from app.graph.build import get_graph
from app.graph.state import new_state
from app.ingress import repository
from app.logging import get_logger
from app.settings import get_settings

log = get_logger(__name__)


async def _load_history(conversation_id: str) -> list[dict[str, str]]:
    """Recent turns for the reply call.

    Phase 04 replaces this with a token-budgeted working set — trimming by
    message count is the naive version and long messages will blow the window.
    """
    limit = get_settings().history_turns
    rows = await db.fetch_all(
        """
        SELECT direction, body FROM messages
        WHERE conversation_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        conversation_id,
        limit,
    )
    return [
        {"role": "customer" if r["direction"] == "in" else "agent", "text": r["body"]}
        for r in reversed(rows)
    ]


async def _persist_slots(conversation_id: str, slots: dict[str, Any]) -> None:
    if not slots:
        return
    for key, value in slots.items():
        await db.execute(
            """
            INSERT INTO slots (conversation_id, key, value, source, updated_at)
            VALUES (%s, %s, %s, 'extracted', now())
            ON CONFLICT (conversation_id, key)
              DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            conversation_id,
            key,
            Json(value),
        )


async def _record_transition(
    conversation_id: str, from_stage: str, to_stage: str, reason: str
) -> None:
    await db.execute(
        """
        INSERT INTO stage_transitions (conversation_id, from_stage, to_stage, reason)
        VALUES (%s, %s, %s, %s)
        """,
        conversation_id,
        from_stage,
        to_stage,
        reason,
    )
    await db.execute("UPDATE conversations SET stage = %s WHERE id = %s", to_stage, conversation_id)


async def run_turn(conversation_id: str, turn_text: str) -> str:
    """Advance one conversation by one turn. Returns the reply text.

    Cancellable throughout — Phase 02 relies on being able to abandon this
    mid-flight when a newer message arrives.
    """
    graph = await get_graph()
    conversation = await repository.get_conversation(conversation_id)
    stage_before = conversation["stage"]

    config = {"configurable": {"thread_id": conversation_id}}

    # The checkpointer supplies prior state; only the turn's own inputs are
    # passed in. Sending `slots` here would overwrite nine days of memory with
    # whatever this process happens to have loaded.
    inputs: dict[str, Any] = {
        "conversation_id": conversation_id,
        "turn_text": turn_text,
        "stage": stage_before,
        "interrupt": None,
        "escalate": False,
        "history": await _load_history(conversation_id),
    }

    snapshot = await graph.aget_state(config)
    if not snapshot.values:
        # First turn: seed the pieces the checkpointer has never seen.
        inputs = {**new_state(conversation_id, stage_before), **inputs}

    result = await graph.ainvoke(inputs, config=config)

    reply = result.get("reply") or ""
    stage_after = result.get("stage") or stage_before
    reason = result.get("route_reason", "unknown")

    await _persist_slots(conversation_id, result.get("slots") or {})

    if result.get("consent"):
        await _persist_slots(conversation_id, {"consent": result["consent"]})

    if stage_after != stage_before:
        await _record_transition(conversation_id, stage_before, stage_after, reason)

    if result.get("escalate"):
        await escalation.record(conversation_id, stage_after, reason, result)
        await db.execute(
            "UPDATE conversations SET status = 'escalated' WHERE id = %s", conversation_id
        )
    elif result.get("slots", {}).get("opted_out"):
        await db.execute(
            "UPDATE conversations SET status = 'opted_out' WHERE id = %s", conversation_id
        )

    usage = result.get("usage") or {}
    log.info(
        "turn_ran",
        stage_before=stage_before,
        stage_after=stage_after,
        reason=reason,
        slots=sorted((result.get("slots") or {}).keys()),
        tokens_in=usage.get("input_tokens"),
        tokens_out=usage.get("output_tokens"),
        model_calls=usage.get("calls"),
    )
    return reply
