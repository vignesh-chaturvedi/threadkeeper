"""Record one row per turn.

Deliberately not OpenTelemetry. The plan allows either, and a collector plus a
backend would be more moving parts than this project has turns — but the real
reason is that the questions here are business questions ("what does a lead cost
before it reaches offers?") rather than latency percentiles, and those want SQL
against rows that outlive a retention window.

A failure to record a trace must never fail the customer's turn. Observability
that can take down the thing it observes is a liability, so `record` swallows and
logs rather than raising.
"""

from __future__ import annotations

from typing import Any

from app import db
from app.logging import get_logger
from app.obs import cost

log = get_logger(__name__)


def turn_usage(before: dict[str, Any] | None, after: dict[str, Any] | None) -> dict[str, int]:
    """Per-turn tokens, from two cumulative counters.

    The graph carries a running total across the whole conversation, so turn 9
    reports the sum of turns 1..9. Subtracting is what turns that back into
    "what did *this* turn cost" — which is the only version that can find an
    expensive turn.

    Clamped at zero: a checkpoint restored from an earlier point would otherwise
    produce a negative token count and a negative cost.
    """
    before = before or {}
    after = after or {}
    return {
        key: max(0, int(after.get(key, 0)) - int(before.get(key, 0)))
        for key in ("input_tokens", "output_tokens", "calls")
    }


async def record(
    conversation_id: str,
    *,
    turn_index: int,
    stage_in: str,
    stage_out: str,
    reason: str,
    intent: str | None,
    held_stage: bool,
    usage: dict[str, int],
    context_tokens: int,
    memory_tiers: list[str],
    latency_ms: int,
    model: str,
    prompt_hash: str | None,
    degraded: bool,
) -> None:
    tokens_in = usage.get("input_tokens", 0)
    tokens_out = usage.get("output_tokens", 0)
    usd = cost.usd_for(model, tokens_in, tokens_out)

    try:
        await db.execute(
            """
            INSERT INTO turns (
              conversation_id, turn_index, stage_in, stage_out, reason, intent,
              held_stage, tokens_in, tokens_out, model_calls, context_tokens,
              memory_tiers, latency_ms, cost_usd, model, prompt_hash, degraded
            )
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (conversation_id, turn_index) DO NOTHING
            """,
            conversation_id,
            turn_index,
            stage_in,
            stage_out,
            reason,
            intent,
            held_stage,
            tokens_in,
            tokens_out,
            usage.get("calls", 0),
            context_tokens,
            memory_tiers,
            latency_ms,
            usd,
            model,
            prompt_hash,
            degraded,
        )
    except Exception as exc:  # noqa: BLE001 — see the module docstring
        log.warning("trace_write_failed", error=str(exc), conversation_id=conversation_id)


async def next_turn_index(conversation_id: str) -> int:
    row = await db.fetch_one(
        "SELECT coalesce(max(turn_index), 0) + 1 AS n FROM turns WHERE conversation_id = %s",
        conversation_id,
    )
    return int(row["n"]) if row else 1
