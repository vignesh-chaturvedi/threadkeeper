"""A small Redis mutex, keyed by conversation.

Why this exists: the debounce logic keeps in-flight asyncio tasks in a
process-local dict. That is correct with one replica and quietly wrong with two
— both processes would happily run a turn for the same conversation and the
customer would get two replies.

The lock makes "one turn at a time per conversation" true across processes. It
is not a distributed-systems showpiece: single Redis, no Redlock, and a TTL so a
crashed holder cannot wedge a conversation forever. That tradeoff is deliberate
and worth saying out loud — at this scale the failure mode of a lost lock is one
duplicated reply, not a lost loan application.
"""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from app.cache import redis
from app.logging import get_logger

log = get_logger(__name__)

# Compare-and-delete. Without this, a holder whose TTL expired mid-turn would
# delete a lock that now belongs to someone else.
_RELEASE = """
if redis.call('get', KEYS[1]) == ARGV[1] then
  return redis.call('del', KEYS[1])
else
  return 0
end
"""


async def acquire(key: str, ttl_s: float) -> str | None:
    """Returns an ownership token, or None if someone else holds the lock."""
    token = secrets.token_hex(12)
    ok = await redis().set(key, token, nx=True, px=int(ttl_s * 1000))
    return token if ok else None


async def release(key: str, token: str) -> bool:
    released = await redis().eval(_RELEASE, 1, key, token)
    return bool(released)


@asynccontextmanager
async def guard(key: str, ttl_s: float) -> AsyncIterator[str | None]:
    """Yields a token if the lock was taken, None if it was already held.

    Callers must check for None. Blocking here would be wrong: if another worker
    already owns this conversation, the right move is to give up, not to queue
    up behind it and send a second reply a moment later.
    """
    token = await acquire(key, ttl_s)
    try:
        yield token
    finally:
        if token is not None:
            await release(key, token)
