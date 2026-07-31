"""Database access for ingress.

The important function here is record_inbound(). It returns None when the
provider has redelivered a message we already stored — and it decides that with
a single INSERT ... ON CONFLICT DO NOTHING RETURNING id, so two webhook
deliveries racing in different workers cannot both win.

A SELECT-then-INSERT would look correct and fail under exactly the conditions
that cause redelivery in the first place.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Json

from app import db
from app.ingress.events import InboundEvent
from app.logging import get_logger

log = get_logger(__name__)


async def get_or_create_conversation(channel: str, customer_ref: str) -> dict[str, Any]:
    """Upsert on (channel, customer_ref).

    DO UPDATE rather than DO NOTHING because DO NOTHING returns no row on
    conflict, which would force a second round trip on every message after the
    first — i.e. on almost all of them.
    """
    row = await db.fetch_one(
        """
        INSERT INTO conversations (channel, customer_ref)
        VALUES (%s, %s)
        ON CONFLICT (channel, customer_ref)
          DO UPDATE SET channel = EXCLUDED.channel
        RETURNING id, channel, customer_ref, stage, status, last_in_at, created_at
        """,
        channel,
        customer_ref,
    )
    assert row is not None  # RETURNING on an upsert always yields a row
    return row


async def get_conversation(conversation_id: str) -> dict[str, Any]:
    row = await db.fetch_one(
        """
        SELECT id, channel, customer_ref, stage, status, last_in_at, created_at
        FROM conversations WHERE id = %s
        """,
        conversation_id,
    )
    if row is None:
        raise LookupError(f"no conversation {conversation_id}")
    return row


async def record_inbound(evt: InboundEvent, conversation_id: str) -> int | None:
    """Store an inbound message. Returns None if it is a redelivery."""
    row = await db.fetch_one(
        """
        INSERT INTO messages (provider_msg_id, conversation_id, direction, body, raw)
        VALUES (%s, %s, 'in', %s, %s)
        ON CONFLICT (provider_msg_id) WHERE provider_msg_id IS NOT NULL
          DO NOTHING
        RETURNING id
        """,
        evt.provider_msg_id,
        conversation_id,
        evt.text,
        Json(evt.raw),
    )
    return row["id"] if row else None


async def touch_last_inbound(conversation_id: str) -> None:
    """Stamps the 24h customer-service window that the scheduler reads.

    Deliberately the scheduler's clock, not SQL `now()`. They are the same thing
    in production, but under a demo clock skip they are not: writing wall time
    here while the worker compares against a skewed clock makes every reply look
    hours old, so a nudge the customer just answered still fires.
    """
    from app.scheduler import clock

    await db.execute(
        "UPDATE conversations SET last_in_at = %s WHERE id = %s",
        await clock.now(),
        conversation_id,
    )


async def record_outbound(
    conversation_id: str, text: str, provider_msg_id: str | None
) -> int | None:
    row = await db.fetch_one(
        """
        INSERT INTO messages (provider_msg_id, conversation_id, direction, body)
        VALUES (%s, %s, 'out', %s)
        RETURNING id
        """,
        provider_msg_id,
        conversation_id,
        text,
    )
    return row["id"] if row else None


async def record_dead_letter(
    conversation_id: str, text: str, attempts: int, last_error: str
) -> None:
    await db.execute(
        """
        INSERT INTO outbound_dead_letters (conversation_id, body, attempts, last_error)
        VALUES (%s, %s, %s, %s)
        """,
        conversation_id,
        text,
        attempts,
        last_error,
    )


async def thread(conversation_id: str, limit: int = 100) -> list[dict[str, Any]]:
    """Newest-last transcript. Used by the simulator and, later, the inspector."""
    rows = await db.fetch_all(
        """
        SELECT id, direction, body, received_at, provider_msg_id
        FROM messages
        WHERE conversation_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        conversation_id,
        limit,
    )
    return list(reversed(rows))
