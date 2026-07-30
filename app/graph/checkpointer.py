"""Create the LangGraph checkpointer's schema.

This deliberately runs as its own step, *after* `alembic upgrade head`, rather
than from inside a migration — which is where it started, and which deadlocks.

Why: `PostgresSaver.setup()` issues `CREATE INDEX CONCURRENTLY`. That statement
waits for every open transaction in the database to finish before it completes.
Called from inside an Alembic migration, the transaction it is waiting on is the
migration itself, which is in turn waiting for setup() to return. Postgres does
not report it as a deadlock because the wait is on a virtualxid rather than a
lock cycle, so it simply hangs — while holding locks that block every other
writer, which is a fairly unpleasant way to discover the problem.

It still is not run at application start. Schema creation belongs in the
deployment path, where it happens once, before any container serves traffic —
not as a side effect of booting, where two replicas would race.

    python -m app.graph.checkpointer
"""

from __future__ import annotations

import asyncio
import sys

from app.logging import configure_logging, get_logger
from app.settings import get_settings

log = get_logger(__name__)


async def setup() -> None:
    """Idempotent and version-aware — safe to run on every deploy."""
    from langgraph.checkpoint.postgres import PostgresSaver

    settings = get_settings()
    # Its own connection, with no surrounding transaction, so CREATE INDEX
    # CONCURRENTLY has nothing to wait on.
    with PostgresSaver.from_conn_string(settings.psycopg_url) as saver:
        await asyncio.to_thread(saver.setup)
    log.info("checkpointer_schema_ready")


def main() -> int:
    configure_logging()
    asyncio.run(setup())
    return 0


if __name__ == "__main__":
    sys.exit(main())
