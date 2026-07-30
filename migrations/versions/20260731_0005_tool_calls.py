"""tool_calls, applications, followups

`tool_calls` is both the audit log and the idempotency mechanism. Same table,
because they are the same question asked twice: "what did this agent do on the
customer's behalf" and "has it already done this one".

The unique index on `idem_key` is what makes a retried `create_application`
return the first application instead of opening a second. Application-level
"check then write" would race exactly when it matters — under a retry storm.

`followups` is created minimally here because `schedule_followup` has to write
somewhere real. Phase 06 owns the ZSET, the backoff policy, quiet hours and the
worker that drains it.

Revision ID: 0005_tool_calls
Revises: 0004_summaries
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_tool_calls"
down_revision: str | None = "0004_summaries"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE tool_calls (
          id              bigserial PRIMARY KEY,
          conversation_id uuid REFERENCES conversations(id) ON DELETE CASCADE,
          tool            text NOT NULL,
          stage_at_call   text NOT NULL,
          idem_key        text,              -- write tools only
          arguments       jsonb NOT NULL,    -- sensitive fields masked before storage
          result          jsonb,
          error           text,
          denied_reason   text,              -- set when the guard refused the call
          latency_ms      integer,
          called_at       timestamptz NOT NULL DEFAULT now()
        )
    """)

    # The idempotency guarantee, in the schema rather than in a handler.
    op.execute("""
        CREATE UNIQUE INDEX tool_calls_idem_key_uq
          ON tool_calls (idem_key) WHERE idem_key IS NOT NULL
    """)
    op.execute("""
        CREATE INDEX tool_calls_conversation_idx
          ON tool_calls (conversation_id, id DESC)
    """)

    op.execute("""
        CREATE TABLE applications (
          id              text PRIMARY KEY,   -- the lender's application id
          conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          offer_id        text NOT NULL,
          lender          text NOT NULL,
          consent_ref     text NOT NULL,      -- proof of what was agreed to
          amount_inr      bigint,
          apr_pct         real,
          status          text NOT NULL DEFAULT 'submitted',
          created_at      timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("""
        CREATE INDEX applications_conversation_idx ON applications (conversation_id)
    """)

    op.execute("""
        CREATE TABLE followups (
          id              bigserial PRIMARY KEY,
          conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          due_at          timestamptz NOT NULL,
          reason          text NOT NULL,
          stage_at_drop   text NOT NULL,
          attempt         integer NOT NULL DEFAULT 0,
          status          text NOT NULL DEFAULT 'pending',
          claimed_at      timestamptz,
          cancelled_reason text,
          created_at      timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT followups_status_ck
            CHECK (status IN ('pending','running','sent','cancelled','exhausted'))
        )
    """)
    # Phase 06's claim loop reads exactly this shape: due, pending, oldest first.
    op.execute("""
        CREATE INDEX followups_due_idx
          ON followups (due_at) WHERE status = 'pending'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS followups")
    op.execute("DROP TABLE IF EXISTS applications")
    op.execute("DROP TABLE IF EXISTS tool_calls")
