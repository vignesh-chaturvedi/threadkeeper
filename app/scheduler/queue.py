"""Durable timers: a Redis ZSET for the poll, Postgres for the truth.

The split the plan asks for, and the reason for it: polling Postgres every two
seconds for due work is a table scan a second, forever, mostly finding nothing.
A ZSET scored by due-time answers "is anything ready?" in O(log n) against an
index built for exactly that question.

But Redis is a cache, and caches get flushed. So Postgres holds every pending
job, and `reconcile()` rebuilds the ZSET from it — which means a flushed Redis
costs one slow tick, not every pending nudge in the system. There is a test that
flushes Redis mid-flight and asserts the nudge still arrives.

Claiming is `FOR UPDATE SKIP LOCKED` against Postgres, never the ZSET. Two
workers popping the same ZSET member is a race; two workers claiming the same
row is something Postgres has solved.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app import db
from app.cache import redis
from app.logging import get_logger
from app.scheduler import clock, policy

log = get_logger(__name__)

ZSET = "tk:followups:due"

# The plan's claim query. The important clause is FOR UPDATE SKIP LOCKED: two
# workers must never send the same nudge, and letting Postgres arbitrate is both
# correct and less code than any lock we could write.
CLAIM = """
UPDATE followups SET status = 'running', claimed_at = now(), updated_at = now()
WHERE id IN (
  SELECT id FROM followups
  WHERE status = 'pending' AND due_at <= %s
  ORDER BY due_at
  FOR UPDATE SKIP LOCKED
  LIMIT %s
)
RETURNING id, conversation_id, attempt, stage_at_drop, reason, due_at
"""


async def schedule(
    conversation_id: str,
    *,
    stage_at_drop: str,
    reason: str = "no_reply",
    attempt: int = 0,
    at: datetime | None = None,
) -> dict[str, Any] | None:
    """Create or move this conversation's pending nudge.

    Upsert rather than insert: the partial unique index permits one pending row
    per conversation, so a customer who sends six messages ends up with one
    nudge that keeps moving, not six that fire together.
    """
    now = await clock.now()
    due_at = at or policy.schedule_at(now, attempt)

    row = await db.fetch_one(
        """
        INSERT INTO followups (conversation_id, due_at, reason, stage_at_drop, attempt)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (conversation_id) WHERE status IN ('pending', 'running')
          DO UPDATE SET due_at = EXCLUDED.due_at,
                        reason = EXCLUDED.reason,
                        stage_at_drop = EXCLUDED.stage_at_drop,
                        status = 'pending',
                        updated_at = now()
        RETURNING id, due_at, attempt
        """,
        conversation_id,
        due_at,
        reason,
        stage_at_drop,
        attempt,
    )
    if row is None:
        return None

    await redis().zadd(ZSET, {str(row["id"]): row["due_at"].timestamp()})
    log.info(
        "followup_scheduled",
        conversation_id=conversation_id,
        due_at=row["due_at"].isoformat(),
        attempt=attempt,
        stage_at_drop=stage_at_drop,
    )
    return dict(row)


async def cancel(conversation_id: str, reason: str) -> int:
    """Drop this conversation's pending nudge. Called when the customer replies."""
    rows = await db.fetch_all(
        """
        UPDATE followups
        SET status = 'cancelled', cancelled_reason = %s, updated_at = now()
        WHERE conversation_id = %s AND status IN ('pending', 'running')
        RETURNING id
        """,
        reason,
        conversation_id,
    )
    if rows:
        await redis().zrem(ZSET, *[str(r["id"]) for r in rows])
        log.info("followup_cancelled", conversation_id=conversation_id, reason=reason, n=len(rows))
    return len(rows)


async def due_soon(now: datetime, limit: int = 50) -> list[str]:
    """Ask Redis whether anything is ready. Cheap, and usually the answer is no."""
    try:
        return await redis().zrangebyscore(ZSET, "-inf", now.timestamp(), start=0, num=limit)
    except Exception:  # noqa: BLE001 — Redis being down must not stop the worker
        log.warning("zset_unavailable_falling_back_to_postgres")
        return []


async def claim(now: datetime, limit: int = 20) -> list[dict[str, Any]]:
    """Take ownership of due jobs. Postgres arbitrates, not Redis."""
    rows = await db.fetch_all(CLAIM, now, limit)
    if rows:
        await redis().zrem(ZSET, *[str(r["id"]) for r in rows])
    return [dict(r) for r in rows]


async def reschedule(followup_id: int, due_at: datetime, *, attempt: int | None = None) -> None:
    row = await db.fetch_one(
        """
        UPDATE followups
        SET status = 'pending', due_at = %s, claimed_at = NULL, updated_at = now(),
            attempt = COALESCE(%s, attempt)
        WHERE id = %s
        RETURNING id, due_at
        """,
        due_at,
        attempt,
        followup_id,
    )
    if row:
        await redis().zadd(ZSET, {str(row["id"]): row["due_at"].timestamp()})


async def mark_sent(followup_id: int, *, template_name: str | None) -> None:
    await db.execute(
        """
        UPDATE followups
        SET status = 'sent', sent_at = now(), template_name = %s, updated_at = now()
        WHERE id = %s
        """,
        template_name,
        followup_id,
    )


async def exhaust(followup_id: int) -> None:
    await db.execute(
        "UPDATE followups SET status = 'exhausted', updated_at = now() WHERE id = %s",
        followup_id,
    )


async def fail(followup_id: int, error: str, retry_at: datetime) -> None:
    """A send that failed is retried, not lost."""
    await db.execute(
        """
        UPDATE followups
        SET status = 'pending', due_at = %s, claimed_at = NULL,
            last_error = %s, updated_at = now()
        WHERE id = %s
        """,
        retry_at,
        error[:500],
        followup_id,
    )
    await redis().zadd(ZSET, {str(followup_id): retry_at.timestamp()})


async def cancel_by_id(followup_id: int, reason: str) -> None:
    await db.execute(
        """
        UPDATE followups
        SET status = 'cancelled', cancelled_reason = %s, updated_at = now()
        WHERE id = %s
        """,
        reason,
        followup_id,
    )
    await redis().zrem(ZSET, str(followup_id))


async def reconcile() -> int:
    """Rebuild the ZSET from Postgres.

    Run at worker start and periodically. This is what makes Redis a cache: a
    FLUSHALL costs one reconciliation, not every pending nudge in the system.
    """
    rows = await db.fetch_all(
        "SELECT id, due_at FROM followups WHERE status = 'pending' ORDER BY due_at LIMIT 10000"
    )
    if not rows:
        return 0
    await redis().zadd(ZSET, {str(r["id"]): r["due_at"].timestamp() for r in rows})
    return len(rows)


async def pending_for(conversation_id: str) -> dict[str, Any] | None:
    row = await db.fetch_one(
        """
        SELECT id, due_at, attempt, reason, stage_at_drop, status
        FROM followups
        WHERE conversation_id = %s AND status IN ('pending', 'running')
        ORDER BY id DESC LIMIT 1
        """,
        conversation_id,
    )
    return dict(row) if row else None
