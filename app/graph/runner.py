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

import time
from typing import Any

from psycopg.types.json import Json

from app import db, memory
from app.graph import escalation, policy, prompts
from app.graph.build import get_graph
from app.graph.state import new_state
from app.ingress import repository
from app.llm import gemini
from app.logging import get_logger
from app.obs import trace
from app.privacy import audit, consent
from app.scheduler import queue
from app.settings import get_settings

log = get_logger(__name__)


async def _persist_slots(
    conversation_id: str, slots: dict[str, Any], sources: dict[str, str] | None = None
) -> None:
    """Mirror state into rows, carrying provenance.

    `source` is not decoration: it is what app.memory.conflict arbitrates on,
    and it is what makes "where did we learn this?" answerable in SQL.
    """
    if not slots:
        return
    sources = sources or {}
    for key, value in slots.items():
        await db.execute(
            """
            INSERT INTO slots (conversation_id, key, value, source, updated_at)
            VALUES (%s, %s, %s, %s, now())
            ON CONFLICT (conversation_id, key)
              DO UPDATE SET value = EXCLUDED.value,
                            source = EXCLUDED.source,
                            updated_at = now()
            """,
            conversation_id,
            key,
            Json(value),
            sources.get(key, "extracted"),
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


async def _record_consent_event(
    conversation_id: str, conversation: dict[str, Any], state: dict[str, Any]
) -> None:
    """Append to the ledger, but only when the decision actually changed.

    The consent slot is rewritten every turn; the ledger must not grow a row per
    turn or the history stops being readable.
    """
    latest = await consent.current(conversation_id)
    granted = bool(state.get("granted"))
    if latest is not None and latest["granted"] == granted:
        return

    await consent.record(
        conversation_id,
        conversation["customer_ref"],
        conversation["channel"],
        event="granted" if granted else "refused",
        wording=prompts.CONSENT_WORDING,
        wording_hash=state.get("wording_hash") or prompts.consent_wording_hash(),
        source="customer_reply",
    )


async def run_turn(conversation_id: str, turn_text: str) -> str:
    """Advance one conversation by one turn. Returns the reply text.

    Cancellable throughout — Phase 02 relies on being able to abandon this
    mid-flight when a newer message arrives.
    """
    started = time.perf_counter()
    gemini.begin_turn()
    graph = await get_graph()
    conversation = await repository.get_conversation(conversation_id)
    stage_before = conversation["stage"]

    config = {"configurable": {"thread_id": conversation_id}}

    # The checkpointer supplies prior state; only the turn's own inputs are
    # passed in. Sending `slots` here would overwrite nine days of memory with
    # whatever this process happens to have loaded.
    snapshot = await graph.aget_state(config)
    prior = snapshot.values or {}

    # Memory is assembled from the tables every turn rather than read out of the
    # checkpoint, so a change to how context is built takes effect immediately
    # instead of only for conversations that start afterwards.
    recollection = await memory.assemble(
        conversation_id,
        conversation["customer_ref"],
        prior.get("slots") or {},
        prior.get("consent") or {},
        turn_text,
    )

    inputs: dict[str, Any] = {
        "conversation_id": conversation_id,
        "turn_text": turn_text,
        "stage": stage_before,
        "interrupt": None,
        "escalate": False,
        "history": recollection.history,
        "profile_block": recollection.profile_block,
        "recall_block": recollection.recall_block,
        "returning": recollection.returning,
    }

    if not prior:
        # First turn: seed the pieces the checkpointer has never seen.
        inputs = {**new_state(conversation_id, stage_before), **inputs}

    result = await graph.ainvoke(inputs, config=config)

    reply = result.get("reply") or ""
    stage_after = result.get("stage") or stage_before
    reason = result.get("route_reason", "unknown")

    await _persist_slots(
        conversation_id, result.get("slots") or {}, result.get("slot_sources") or {}
    )

    if result.get("consent"):
        # Consent is always 'confirmed': it only ever comes from the customer
        # answering the consent question directly.
        await _persist_slots(
            conversation_id, {"consent": result["consent"]}, {"consent": "confirmed"}
        )
        # And it goes into the ledger, once, with the exact wording shown. The
        # slot drives routing; the ledger is the evidence.
        await _record_consent_event(conversation_id, conversation, result["consent"])

    # Revocation is not a stage transition — it is an instruction, and it takes
    # effect in this call rather than on the scheduler's next pass.
    if result.get("slots", {}).get("opted_out") and await consent.is_granted(conversation_id):
        await consent.revoke(conversation_id)

    if stage_after != stage_before:
        await _record_transition(conversation_id, stage_before, stage_after, reason)

    if result.get("application"):
        # The one status that means success. It was in the schema's CHECK
        # constraint from Phase 00 and nothing set it, because until the funnel
        # gained a close-on-acceptance path there was nothing to set it from —
        # which is why the console's first render showed a closed sale as
        # 'active'.
        await db.execute("UPDATE conversations SET status = 'won' WHERE id = %s", conversation_id)
    elif result.get("escalate"):
        await escalation.record(conversation_id, stage_after, reason, result)
        await db.execute(
            "UPDATE conversations SET status = 'escalated' WHERE id = %s", conversation_id
        )
    elif result.get("slots", {}).get("opted_out"):
        await db.execute(
            "UPDATE conversations SET status = 'opted_out' WHERE id = %s", conversation_id
        )

    # A conversation that has ended gets summarised and embedded once, for
    # tier 3 recall the next time this customer comes back.
    if policy.is_terminal(stage_after) and stage_after != stage_before:
        await memory.semantic.store(conversation_id)

    # --- the follow-up, rearmed every turn -------------------------------
    # The customer just spoke, so any pending nudge is answered and cancelled.
    # If the conversation is still live, a fresh one is armed for the moment
    # they go quiet. A terminal conversation gets neither.
    await queue.cancel(conversation_id, "customer_replied")
    if not policy.is_terminal(stage_after) and not result.get("slots", {}).get("opted_out"):
        await queue.schedule(conversation_id, stage_at_drop=stage_after, reason="no_reply")

    # The audit entry carries the prompt hash and model, which is what makes a
    # reply reproducible six months later rather than merely recorded.
    settings = get_settings()
    await audit.write(
        conversation_id,
        "turn",
        stage=stage_after,
        prompt_hash=prompts.prompt_hash(),
        model=(settings.gemini_reply_model if settings.llm_provider == "gemini" else "fake"),
        detail={
            "stage_before": stage_before,
            "reason": reason,
            "slots_known": sorted((result.get("slots") or {}).keys()),
            "memory_tiers": recollection.tiers,
            "usage": result.get("usage") or {},
        },
    )

    # --- the trace row ----------------------------------------------------
    # Written last, so it records what actually happened rather than what was
    # about to be attempted, and after the latency it reports has been incurred.
    # `usage` on the state is cumulative for the conversation; the turn's own
    # cost is the difference across this invocation.
    await trace.record(
        conversation_id,
        turn_index=await trace.next_turn_index(conversation_id),
        stage_in=stage_before,
        stage_out=stage_after,
        reason=reason,
        intent=result.get("intent"),
        held_stage=bool(result.get("holds_stage")),
        usage=trace.turn_usage(prior.get("usage"), result.get("usage")),
        context_tokens=recollection.tokens_used,
        memory_tiers=list(recollection.tiers),
        # Elapsed minus our own throttle. A rate limiter is a decision about
        # spend, not a property of the model, and leaving it in makes "which
        # stage is slow" unanswerable — one turn read 50s, of which 47 was the
        # free-tier pacing this project chose to apply.
        latency_ms=max(0, int((time.perf_counter() - started) * 1000) - gemini.throttled_ms()),
        model=(settings.gemini_reply_model if settings.llm_provider == "gemini" else "fake"),
        prompt_hash=prompts.prompt_hash(),
        degraded=reason == "model_unavailable",
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
        memory_tiers=recollection.tiers,
        context_tokens=recollection.tokens_used,
    )
    return reply
