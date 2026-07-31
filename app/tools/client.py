"""The one path every tool call takes.

Guard, then idempotency, then invoke, then record. In that order, and there is
no way around it: `app/tools/server.py` (MCP, for external callers) and the
graph both call `invoke()`, so a rule added here applies to both without either
having to remember.

On idempotency: the key is checked *and* the result stored in one table with a
unique index, so a retried `create_application` returns the original
application. The obvious implementation — look up the key, then write if absent
— loses precisely the race that retries create.

On why write tools return their previous result rather than erroring: a retry is
usually a network timeout on a call that actually succeeded. The caller wants
the outcome, not an argument about who saw what.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from psycopg.types.json import Json

from app import db
from app.logging import get_logger
from app.privacy import audit
from app.tools import guard, registry

log = get_logger(__name__)


class ToolDenied(Exception):
    """The guard refused. Not an error condition — a policy outcome."""

    def __init__(self, tool: str, reason: str) -> None:
        super().__init__(f"{tool} denied: {reason}")
        self.tool = tool
        self.reason = reason


def derive_idem_key(conversation_id: str, tool: str, arguments: dict[str, Any]) -> str:
    """Deterministic from the intent, so a retry of the same intent collides.

    Deliberately not random: a random key per attempt would make every retry a
    new application, which is the bug idempotency exists to prevent.
    """
    material = json.dumps(
        {"c": conversation_id, "t": tool, "a": dict(sorted(arguments.items()))},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()[:32]


async def _replay(idem_key: str) -> dict[str, Any] | None:
    row = await db.fetch_one(
        "SELECT result, error FROM tool_calls WHERE idem_key = %s AND result IS NOT NULL",
        idem_key,
    )
    return row["result"] if row else None


async def _record(
    conversation_id: str | None,
    tool: str,
    stage: str,
    arguments: dict[str, Any],
    *,
    idem_key: str | None = None,
    result: dict[str, Any] | None = None,
    error: str | None = None,
    denied_reason: str | None = None,
    latency_ms: int | None = None,
) -> None:
    await db.execute(
        """
        INSERT INTO tool_calls
          (conversation_id, tool, stage_at_call, idem_key, arguments,
           result, error, denied_reason, latency_ms)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (idem_key) WHERE idem_key IS NOT NULL DO NOTHING
        """,
        conversation_id,
        tool,
        stage,
        idem_key,
        Json(registry.mask(arguments)),
        Json(result) if result is not None else None,
        error,
        denied_reason,
        latency_ms,
    )


async def invoke(
    tool: str,
    arguments: dict[str, Any],
    *,
    stage: str,
    state: dict[str, Any] | None = None,
    conversation_id: str | None = None,
) -> dict[str, Any]:
    """Call a tool, subject to the guard. Never raises for a tool's own failure."""
    state = state or {}
    conversation_id = conversation_id or arguments.get("conversation_id")

    if tool not in registry.TOOLS:
        return {"error": "unknown_tool", "retryable": False}

    # --- 1. the guard ---------------------------------------------------
    verdict = guard.check(tool, stage, state)
    if not verdict:
        log.warning("tool_denied", tool=tool, stage=stage, reason=verdict.reason)
        await _record(conversation_id, tool, stage, arguments, denied_reason=verdict.reason)
        await audit.write(
            conversation_id,
            "tool_call",
            stage=stage,
            detail={"tool": tool, "denied": verdict.reason},
        )
        return {"error": "tool_not_permitted", "reason": verdict.reason, "retryable": False}

    # --- 2. idempotency, for write tools --------------------------------
    idem_key: str | None = None
    if tool in guard.WRITE_TOOLS:
        idem_key = arguments.get("idem_key") or derive_idem_key(
            conversation_id or "", tool, {k: v for k, v in arguments.items() if k != "idem_key"}
        )
        arguments = {**arguments, "idem_key": idem_key}

        previous = await _replay(idem_key)
        if previous is not None:
            log.info("tool_replayed", tool=tool, idem_key=idem_key)
            return {**previous, "idempotent_replay": True}

    # --- 3. invoke ------------------------------------------------------
    started = time.perf_counter()
    try:
        result = await registry.TOOLS[tool](**arguments)
        error = None
    except Exception as exc:  # a tool crash must not kill a turn
        log.exception("tool_crashed", tool=tool)
        result, error = {"error": "tool_failed", "retryable": False}, str(exc)
    latency_ms = int((time.perf_counter() - started) * 1000)

    # --- 4. record ------------------------------------------------------
    await _record(
        conversation_id,
        tool,
        stage,
        arguments,
        idem_key=idem_key,
        result=result,
        error=error,
        latency_ms=latency_ms,
    )
    await audit.write(
        conversation_id,
        "tool_call",
        stage=stage,
        detail={
            "tool": tool,
            "ok": "error" not in result,
            "error": result.get("error"),
            "latency_ms": latency_ms,
            "idem_key": idem_key,
        },
    )
    log.info(
        "tool_called",
        tool=tool,
        stage=stage,
        latency_ms=latency_ms,
        ok="error" not in result,
    )
    return result


async def calls_for(conversation_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """The audit trail for one conversation. Used by the simulator and console."""
    rows = await db.fetch_all(
        """
        SELECT tool, stage_at_call, result, error, denied_reason, latency_ms, called_at
        FROM tool_calls WHERE conversation_id = %s ORDER BY id DESC LIMIT %s
        """,
        conversation_id,
        limit,
    )
    return [
        {
            "tool": r["tool"],
            "stage": r["stage_at_call"],
            "ok": r["error"] is None and r["denied_reason"] is None,
            "denied": r["denied_reason"],
            "latency_ms": r["latency_ms"],
            "at": r["called_at"].isoformat(),
        }
        for r in reversed(rows)
    ]
