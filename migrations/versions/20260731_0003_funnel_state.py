"""slots, stage_transitions and escalations

Two things happen here.

**Our tables.** `slots` holds one row per known fact with provenance, so
"where did we learn the customer's income?" is answerable. `stage_transitions`
is append-only and becomes the funnel chart in Phase 10 — every advance records
*which exit condition fired*, which is what makes the funnel auditable rather
than merely observable.

**Not LangGraph's tables.** Those are created by `app.graph.checkpointer`,
which the migrate service runs immediately after this. They cannot live in a
migration: `PostgresSaver.setup()` issues `CREATE INDEX CONCURRENTLY`, which
waits for every open transaction to finish — including the migration's own — and
hangs forever while holding locks that block every other writer.

Revision ID: 0003_funnel_state
Revises: 0002_conversations
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_funnel_state"
down_revision: str | None = "0002_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE slots (            -- one row per fact, with provenance
          conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          key         text NOT NULL,    -- product | income_band | pan_status | city_tier | objection
          value       jsonb NOT NULL,
          confidence  real NOT NULL DEFAULT 1.0,
          source      text NOT NULL DEFAULT 'extracted',  -- extracted | confirmed | api
          updated_at  timestamptz NOT NULL DEFAULT now(),
          PRIMARY KEY (conversation_id, key),
          CONSTRAINT slots_source_ck CHECK (source IN ('extracted','confirmed','api')),
          CONSTRAINT slots_confidence_ck CHECK (confidence >= 0 AND confidence <= 1)
        )
    """)

    op.execute("""
        CREATE TABLE stage_transitions ( -- append-only: this becomes the funnel chart
          id              bigserial PRIMARY KEY,
          conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          from_stage      text,
          to_stage        text NOT NULL,
          reason          text NOT NULL,   -- which exit condition fired
          at              timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX stage_transitions_conversation_idx
          ON stage_transitions (conversation_id, id)
    """)

    op.execute("""
        CREATE TABLE escalations (       -- the packet a human picks up
          id              bigserial PRIMARY KEY,
          conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          stage_at_escalation text NOT NULL,
          reason          text NOT NULL,
          packet          jsonb NOT NULL, -- transcript, slots, intent, last offer shown
          created_at      timestamptz NOT NULL DEFAULT now(),
          resolved_at     timestamptz
        )
    """)
    op.execute("""
        CREATE INDEX escalations_open_idx
          ON escalations (created_at DESC) WHERE resolved_at IS NULL
    """)


def downgrade() -> None:
    # LangGraph's own tables are left alone — dropping them would discard every
    # conversation's durable state, which is not what a downgrade should mean.
    op.execute("DROP TABLE IF EXISTS escalations")
    op.execute("DROP TABLE IF EXISTS stage_transitions")
    op.execute("DROP TABLE IF EXISTS slots")
