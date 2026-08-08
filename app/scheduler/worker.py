"""The follow-up worker.

Their homepage promise — "drop-offs resume with context, timing, and the next
action" — is entirely this loop. It is also the least glamorous component in the
project, which is exactly why building it properly is worth doing.

One pass:

    claim due jobs (SKIP LOCKED)
      → should this still be sent?      cancel if they replied, opted out, escalated
      → is it a reasonable hour in IST? reschedule to 09:00 if not
      → template or free-form?          the 24-hour service window decides
      → send, then reschedule or exhaust

Every one of those is a separate, testable decision, and all the timing rules
live in `policy.py` rather than here. This file is the sequencing.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from typing import Any

from app import cache, db
from app.ingress import outbound
from app.ingress.events import OutboundMessage
from app.logging import configure_logging, get_logger, log_context
from app.scheduler import clock, policy, queue, reentry
from app.settings import get_settings

log = get_logger(__name__)

RECONCILE_INTERVAL_S = 300.0  # ~5 minutes, regardless of how fast we poll


async def process(job: dict[str, Any]) -> str:
    """Handle one claimed follow-up. Returns what happened, for the logs."""
    now = await clock.now()
    conversation_id = str(job["conversation_id"])

    with log_context(conversation_id=conversation_id, followup_id=job["id"]):
        conversation = await db.fetch_one(
            "SELECT id, channel, customer_ref, stage, status, last_in_at "
            "FROM conversations WHERE id = %s",
            conversation_id,
        )
        if conversation is None:
            await queue.cancel_by_id(job["id"], "conversation_missing")
            return "conversation_missing"

        # --- should this still go out at all? ----------------------------
        reason = policy.cancellation_reason(
            conversation["status"], conversation["last_in_at"], job["due_at"]
        )
        if reason:
            await queue.cancel_by_id(job["id"], reason)
            log.info("followup_dropped", reason=reason)
            return reason

        # --- is it a civil hour in IST? ----------------------------------
        slot = policy.next_allowed_slot(now)
        if slot > now:
            await queue.reschedule(job["id"], slot)
            log.info("followup_deferred_quiet_hours", until=slot.isoformat())
            return "quiet_hours"

        # --- template or free-form? --------------------------------------
        in_window = policy.within_service_window(now, conversation["last_in_at"])
        text, template_name = await reentry.compose(
            conversation_id,
            job["stage_at_drop"],
            use_template=not in_window,
            attempt=job["attempt"],
        )

        result = await outbound.send(
            OutboundMessage(
                channel=conversation["channel"],
                conversation_id=conversation_id,
                customer_ref=conversation["customer_ref"],
                text=text,
                template_name=template_name,
            )
        )
        if result.provider_msg_id is None:
            # The send failed and has already been dead-lettered by the sender.
            # Retry the nudge later rather than burning an attempt on it.
            retry_at = policy.next_allowed_slot(now + policy.delay_for(job["attempt"]))
            await queue.fail(job["id"], "send_failed", retry_at)
            return "send_failed"

        await queue.mark_sent(job["id"], template_name=template_name)

        # --- schedule the next attempt, or stop --------------------------
        attempt = job["attempt"] + 1
        if policy.is_exhausted(attempt):
            await queue.exhaust(job["id"])
            log.info("followup_exhausted", attempts=attempt)
            return "sent_and_exhausted"

        await queue.schedule(
            conversation_id,
            stage_at_drop=job["stage_at_drop"],
            reason=job["reason"],
            attempt=attempt,
        )
        log.info(
            "followup_sent",
            attempt=attempt,
            in_service_window=in_window,
            template=template_name,
        )
        return "sent"


async def tick() -> int:
    """One pass of the scheduler. Returns jobs processed."""
    now = await clock.now()

    # Ask Redis first — it usually says "nothing due" in microseconds. The
    # answer is advisory only: Postgres is the truth, so a cold or flushed ZSET
    # costs an extra claim query rather than a missed nudge.
    peek = await queue.due_soon(now, limit=1)
    jobs = await queue.claim(now, limit=20)
    if jobs and not peek:
        log.info("zset_cold", claimed=len(jobs))

    for job in jobs:
        try:
            await process(job)
        except Exception:
            # One bad job must never stop the loop; that would silently freeze
            # every follow-up in the system.
            log.exception("followup_failed", followup_id=job.get("id"))
            with contextlib.suppress(Exception):
                await queue.fail(job["id"], "worker_exception", await clock.now())
    return len(jobs)


async def poll_loop(stopping: asyncio.Event | None = None) -> None:
    """The claim loop itself. Assumes both stores are already open.

    Split out from `run` so the API process can host it as a task without also
    inheriting the store lifecycle and the signal handlers, both of which the
    app's own lifespan already owns. Two callers, one loop — the same shape as
    the tool registry having an MCP door and an in-process one.
    """
    stopping = stopping or asyncio.Event()
    interval = get_settings().scheduler_poll_interval_s

    restored = await queue.reconcile()
    log.info(
        "worker_started",
        poll_interval_s=interval,
        restored_to_zset=restored,
        backoff=[str(d) for d in policy.BACKOFF],
        quiet_hours_ist=f"{policy.QUIET_START_HOUR}:00-{policy.QUIET_END_HOUR}:00",
    )

    # Reconcile on a wall-clock schedule rather than a tick count. Counting ticks
    # was fine when the interval was a constant; with it configurable, "every 150
    # ticks" silently becomes every 75 minutes at a 30s poll.
    next_reconcile = time.monotonic() + RECONCILE_INTERVAL_S
    try:
        while not stopping.is_set():
            try:
                processed = await tick()
                if processed:
                    log.info("worker_batch", processed=processed)
                if time.monotonic() >= next_reconcile:
                    await queue.reconcile()
                    next_reconcile = time.monotonic() + RECONCILE_INTERVAL_S
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("worker_tick_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=interval)
    finally:
        log.info("worker_stopping")


async def run() -> None:
    """Standalone entrypoint: owns the stores and the signals, then polls."""
    configure_logging()
    await db.open_pool()
    await cache.open_redis()

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)

    try:
        await poll_loop(stopping)
    finally:
        await cache.close_redis()
        await db.close_pool()


def main() -> None:
    settings = get_settings()
    log.info("scheduler_config", env=settings.env)
    asyncio.run(run())


if __name__ == "__main__":
    main()


# `tick` and `process` are exported so tests can run one pass without the loop.
__all__ = ["process", "run", "tick"]
