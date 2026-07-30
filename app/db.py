"""One psycopg3 async connection pool for the whole process.

Deliberately not SQLAlchemy ORM. Two reasons:
  1. LangGraph's Postgres checkpointer (Phase 03) is built on psycopg3. Running
     one driver instead of two means one pool, one set of timeouts, one failure
     mode to reason about.
  2. Most of the queries in this system are things the ORM would get in the way
     of anyway — ON CONFLICT DO NOTHING, FOR UPDATE SKIP LOCKED, jsonb.

SQLAlchemy is still a dependency, but only because Alembic needs it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from app.logging import get_logger
from app.settings import get_settings

log = get_logger(__name__)

_pool: AsyncConnectionPool | None = None


async def open_pool() -> AsyncConnectionPool:
    """Idempotent. Called from the FastAPI lifespan and from the worker's main()."""
    global _pool
    if _pool is not None:
        return _pool

    settings = get_settings()
    _pool = AsyncConnectionPool(
        conninfo=settings.psycopg_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        kwargs={"row_factory": dict_row, "autocommit": True},
        open=False,
    )
    await _pool.open(wait=True, timeout=30)
    log.info("db_pool_open", min=settings.db_pool_min, max=settings.db_pool_max)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        log.info("db_pool_closed")


def pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("db pool not open — call open_pool() in the app lifespan first")
    return _pool


@asynccontextmanager
async def connection() -> AsyncIterator[Any]:
    async with pool().connection() as conn:
        yield conn


async def fetch_all(sql: str, *params: Any) -> list[dict[str, Any]]:
    async with connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params or None)
        return await cur.fetchall()


async def fetch_one(sql: str, *params: Any) -> dict[str, Any] | None:
    async with connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params or None)
        return await cur.fetchone()


async def execute(sql: str, *params: Any) -> int:
    async with connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, params or None)
        return cur.rowcount


async def ping() -> bool:
    """Used by /health/ready. Cheap enough to call on every probe."""
    try:
        row = await fetch_one("SELECT 1 AS ok")
        return bool(row and row["ok"] == 1)
    except Exception as exc:  # noqa: BLE001 — a readiness probe must never raise
        log.warning("db_ping_failed", error=str(exc))
        return False
