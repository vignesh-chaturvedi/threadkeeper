"""Follow-up worker entrypoint.

Phase 00 stands the process up so `docker compose up` brings the full topology
online and the deployment shape is fixed early. The claim loop (Redis ZSET +
`FOR UPDATE SKIP LOCKED` over the `followups` table) arrives in Phase 06.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

from app import cache, db
from app.logging import configure_logging, get_logger

log = get_logger(__name__)

POLL_INTERVAL_S = 2.0


async def tick() -> int:
    """One pass of the scheduler. Returns jobs processed. Phase 06 fills this in."""
    return 0


async def run() -> None:
    configure_logging()
    await db.open_pool()
    await cache.open_redis()

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)

    log.info("worker_started", poll_interval_s=POLL_INTERVAL_S)
    try:
        while not stopping.is_set():
            try:
                processed = await tick()
                if processed:
                    log.info("worker_batch", processed=processed)
            except Exception:
                # A bad job must never kill the loop — that would silently stop
                # every follow-up in the system.
                log.exception("worker_tick_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stopping.wait(), timeout=POLL_INTERVAL_S)
    finally:
        log.info("worker_stopping")
        await cache.close_redis()
        await db.close_pool()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
