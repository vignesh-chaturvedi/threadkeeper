"""A clock the demo can move.

Nobody will wait three days to watch a nudge fire, and a scheduler you cannot
demonstrate may as well not exist. So every time the scheduler asks what time it
is, it asks here, and here adds an offset.

The offset lives in **Redis, not a process variable**, because the API and the
worker are different containers. A skip triggered from the simulator has to be
visible to the worker or the demo shows nothing.

Guarded to local and test. In staging or prod `skip()` refuses and `now()` is
`datetime.now(UTC)` with no indirection worth thinking about — a schedulable
system whose clock can be moved by an HTTP call is not one you deploy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.cache import redis
from app.logging import get_logger
from app.settings import get_settings

log = get_logger(__name__)

KEY = "tk:clock_offset_s"


class ClockSkipRefused(RuntimeError):
    """Someone tried to move the clock outside local/test."""


def _enabled() -> bool:
    return get_settings().env in ("local", "test")


async def offset() -> timedelta:
    if not _enabled():
        return timedelta(0)
    try:
        raw = await redis().get(KEY)
    except Exception:  # noqa: BLE001 — the clock must never take the worker down
        return timedelta(0)
    return timedelta(seconds=float(raw)) if raw else timedelta(0)


async def now() -> datetime:
    """The scheduler's idea of the current time."""
    return datetime.now(UTC) + await offset()


async def skip(delta: timedelta) -> timedelta:
    """Jump the clock forward. Cumulative, so two skips add up."""
    if not _enabled():
        raise ClockSkipRefused("clock skipping is disabled outside local/test")
    current = await offset()
    new = current + delta
    await redis().set(KEY, str(new.total_seconds()))
    log.warning(
        "clock_skipped",
        by_seconds=delta.total_seconds(),
        total_offset_hours=round(new.total_seconds() / 3600, 2),
    )
    return new


async def reset() -> None:
    if not _enabled():
        raise ClockSkipRefused("clock skipping is disabled outside local/test")
    await redis().delete(KEY)
    log.warning("clock_reset")
