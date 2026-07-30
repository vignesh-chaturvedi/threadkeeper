"""conversation_summaries — tier 3 semantic recall

One row per *closed conversation*, not per message. That is the whole design
decision: embedding every message retrieves greetings. "hi", "ok", "haan" are
the most common things anyone says and they carry no information, so a
per-message index spends its top-k on noise. A summary written once at close is
the unit that actually answers "what happened with this customer last time".

Scoped to `customer_ref`, because the question retrieval earns its keep on is
"what did *this* customer complain about", not "what do customers complain
about" — the latter is analytics, and belongs in SQL.

Revision ID: 0004_summaries
Revises: 0003_funnel_state
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0004_summaries"
down_revision: str | None = "0003_funnel_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Must match TK_EMBEDDING_DIMENSIONS. pgvector needs this fixed at DDL time,
# which is why the setting is documented as a schema decision rather than a knob.
DIMS = 768


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE conversation_summaries (
          id              bigserial PRIMARY KEY,
          conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          customer_ref    text NOT NULL,
          channel         text NOT NULL,
          summary         text NOT NULL,
          objections      jsonb NOT NULL DEFAULT '[]'::jsonb,
          outcome         text NOT NULL,        -- won | lost | opted_out | escalated | stalled
          final_stage     text NOT NULL,
          embedding       vector({DIMS}),
          created_at      timestamptz NOT NULL DEFAULT now(),
          UNIQUE (conversation_id)
        )
    """)

    # Retrieval always filters by customer first, so this composite index does
    # more work than the vector index will at this scale.
    op.execute("""
        CREATE INDEX conversation_summaries_customer_idx
          ON conversation_summaries (customer_ref, created_at DESC)
    """)

    # IVFFlat needs training data to be useful and is counterproductive on a
    # small table; HNSW builds usefully from empty. Either way, with a customer
    # filter this is rarely the deciding index — which is itself part of the
    # argument that retrieval is not where the value is.
    op.execute("""
        CREATE INDEX conversation_summaries_embedding_idx
          ON conversation_summaries USING hnsw (embedding vector_cosine_ops)
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS conversation_summaries")
