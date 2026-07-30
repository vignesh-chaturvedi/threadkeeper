"""conversations, messages, outbound dead letters

The idempotency guarantee for Phase 01 lives in this file, not in application
code: a partial unique index on messages.provider_msg_id. The webhook handler
does INSERT ... ON CONFLICT DO NOTHING RETURNING id, so the database — not a
read-then-write check that races under concurrent redelivery — decides whether a
message has been seen.

Slots and stage_transitions arrive in Phase 03 with the stage machine.

Revision ID: 0002_conversations
Revises: 0001_baseline
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0002_conversations"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE conversations (
          id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          channel       text        NOT NULL,
          customer_ref  text        NOT NULL,   -- HMAC of the phone number, never the digits
          stage         text        NOT NULL DEFAULT 'intent_route',
          status        text        NOT NULL DEFAULT 'active',
          last_in_at    timestamptz,            -- drives the 24h messaging window in Phase 06
          created_at    timestamptz NOT NULL DEFAULT now(),
          UNIQUE (channel, customer_ref),
          CONSTRAINT conversations_status_ck
            CHECK (status IN ('active','won','lost','opted_out','escalated'))
        )
    """)

    op.execute("""
        CREATE TABLE messages (
          id               bigserial PRIMARY KEY,
          conversation_id  uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          provider_msg_id  text,                -- NULL for outbound that never reached the provider
          direction        text NOT NULL,
          body             text NOT NULL,
          raw              jsonb,
          received_at      timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT messages_direction_ck CHECK (direction IN ('in','out'))
        )
    """)

    # Partial, because outbound rows legitimately carry NULL until the provider
    # accepts them. Postgres permits multiple NULLs under a plain UNIQUE too, but
    # the predicate states the intent and keeps the index smaller.
    op.execute("""
        CREATE UNIQUE INDEX messages_provider_msg_id_uq
          ON messages (provider_msg_id)
          WHERE provider_msg_id IS NOT NULL
    """)

    op.execute("""
        CREATE INDEX messages_conversation_recent_idx
          ON messages (conversation_id, received_at DESC)
    """)

    # Failed sends must be visible, not silently lost.
    op.execute("""
        CREATE TABLE outbound_dead_letters (
          id               bigserial PRIMARY KEY,
          conversation_id  uuid REFERENCES conversations(id) ON DELETE CASCADE,
          body             text NOT NULL,
          attempts         int  NOT NULL,
          last_error       text,
          failed_at        timestamptz NOT NULL DEFAULT now()
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS outbound_dead_letters")
    op.execute("DROP TABLE IF EXISTS messages")
    op.execute("DROP TABLE IF EXISTS conversations")
