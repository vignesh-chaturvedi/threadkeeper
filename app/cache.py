"""Redis client. Phase 00 only needs it for the readiness probe; Phase 02 (turn
buffer) and Phase 06 (follow-up ZSET) are the real consumers.
"""

from __future__ import annotations

from redis.asyncio import Redis

from app.logging import get_logger
from app.settings import get_settings

log = get_logger(__name__)

_redis: Redis | None = None


async def open_redis() -> Redis:
    global _redis
    if _redis is not None:
        return _redis
    _redis = Redis.from_url(str(get_settings().redis_url), decode_responses=True)
    await _redis.ping()
    log.info("redis_open")
    return _redis


async def close_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
        log.info("redis_closed")


def redis() -> Redis:
    if _redis is None:
        raise RuntimeError("redis not open — call open_redis() in the app lifespan first")
    return _redis


async def ping() -> bool:
    try:
        return bool(await redis().ping())
    except Exception as exc:  # noqa: BLE001 — a readiness probe must never raise
        log.warning("redis_ping_failed", error=str(exc))
        return False
