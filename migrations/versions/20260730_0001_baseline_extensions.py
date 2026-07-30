"""baseline: extensions only, no domain tables yet

Phase 00's migration deliberately creates zero application tables. It exists to
prove the migration path works end to end before there is anything to lose, and
to turn on the two extensions every later phase assumes:

  * pgcrypto — gen_random_uuid() for conversation ids
  * vector   — pgvector, used by the Tier 3 memory in Phase 04

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-30
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")


def downgrade() -> None:
    # Not dropped: other schemas in the same database may depend on them, and a
    # downgrade that breaks unrelated objects is worse than a leftover extension.
    pass
