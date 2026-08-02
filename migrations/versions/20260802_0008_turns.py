"""per-turn traces

`stage_transitions` records the moves; it says nothing about the turns that did
not move. A customer who sends four messages while stuck in `qualify` produces
zero transition rows, so the table that was supposed to explain the funnel is
silent about exactly the turns where people give up.

This table records every turn, moved or not, with what it cost. Denormalised on
purpose: tokens and latency are facts about one turn, and recomputing them by
joining the audit log to the checkpoint every time the console loads is how a
dashboard becomes something nobody opens.

Revision ID: 0008_turns
Revises: 0007_privacy
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0008_turns"
down_revision: str | None = "0007_privacy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE turns (
          id              bigserial PRIMARY KEY,
          conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          turn_index      integer NOT NULL,   -- 1-based, per conversation
          stage_in        text NOT NULL,
          stage_out       text NOT NULL,
          reason          text NOT NULL,      -- which exit condition fired
          intent          text,               -- the Phase 09 taxonomy, per turn
          held_stage      boolean NOT NULL DEFAULT false,

          -- Tokens are per turn, not cumulative. The graph state carries a
          -- running total; storing that here would make every chart a
          -- monotonically increasing line and "the expensive turn" unfindable.
          tokens_in       integer NOT NULL DEFAULT 0,
          tokens_out      integer NOT NULL DEFAULT 0,
          model_calls     integer NOT NULL DEFAULT 0,
          context_tokens  integer NOT NULL DEFAULT 0,
          memory_tiers    text[]  NOT NULL DEFAULT '{}',

          latency_ms      integer NOT NULL DEFAULT 0,
          -- Priced at write time, from the model that actually ran. Recomputing
          -- historic cost against today's price list would silently rewrite what
          -- last month cost every time the vendor changes a number.
          cost_usd        numeric(12, 8) NOT NULL DEFAULT 0,
          model           text NOT NULL,
          prompt_hash     text,

          degraded        boolean NOT NULL DEFAULT false,  -- model failed, replied anyway
          at              timestamptz NOT NULL DEFAULT now(),
          UNIQUE (conversation_id, turn_index)
        )
    """)
    op.execute("""
        CREATE INDEX turns_conversation_idx ON turns (conversation_id, turn_index)
    """)
    # The console's default view: most recent activity first.
    op.execute("CREATE INDEX turns_at_idx ON turns (at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS turns")
