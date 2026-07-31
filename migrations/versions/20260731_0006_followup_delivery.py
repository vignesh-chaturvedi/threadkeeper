"""followups: delivery columns + one-pending-per-conversation

Two changes.

**Delivery detail.** `sent_at`, `template_name` and `last_error` — a scheduler
you cannot audit after the fact is one you cannot debug. Whether a nudge went
out as a template or free-form is exactly the question a WhatsApp policy review
asks.

**A partial unique index.** At most one *pending* follow-up per conversation.
Without it, every inbound turn that schedules a nudge leaves the previous one
behind, and a customer who sends five messages accumulates five pending nudges
that all fire at once when they go quiet. Enforced by the database rather than
by remembering to cancel first.

Revision ID: 0006_followup_delivery
Revises: 0005_tool_calls
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0006_followup_delivery"
down_revision: str | None = "0005_tool_calls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE followups
          ADD COLUMN sent_at       timestamptz,
          ADD COLUMN template_name text,
          ADD COLUMN last_error    text,
          ADD COLUMN updated_at    timestamptz NOT NULL DEFAULT now()
    """)

    # One live nudge per conversation. Rescheduling updates the row in place;
    # a customer replying cancels it. Both leave exactly one row satisfying the
    # predicate, and a bug that tried to create a second would fail loudly here
    # rather than quietly double-messaging someone.
    op.execute("""
        CREATE UNIQUE INDEX followups_one_pending_per_conversation
          ON followups (conversation_id)
          WHERE status IN ('pending', 'running')
    """)

    # The Phase 10 funnel view wants "how many nudges went out, and did they work".
    op.execute("""
        CREATE INDEX followups_sent_idx ON followups (sent_at DESC) WHERE sent_at IS NOT NULL
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS followups_sent_idx")
    op.execute("DROP INDEX IF EXISTS followups_one_pending_per_conversation")
    op.execute("""
        ALTER TABLE followups
          DROP COLUMN IF EXISTS updated_at,
          DROP COLUMN IF EXISTS last_error,
          DROP COLUMN IF EXISTS template_name,
          DROP COLUMN IF EXISTS sent_at
    """)
