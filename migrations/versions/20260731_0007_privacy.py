"""pii_vault, consent_ledger, audit_log — and triggers that make two of them immutable

"Append-only" is usually a convention, which means it holds until someone writes
an UPDATE. Here it is a trigger: the database refuses. An auditor asking "could
a consent record have been altered after the fact?" gets a better answer than
"we don't do that".

Revision ID: 0007_privacy
Revises: 0006_followup_delivery
Create Date: 2026-07-31
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0007_privacy"
down_revision: str | None = "0006_followup_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ----------------------------------------------------------------- vault
    op.execute("""
        CREATE TABLE pii_vault (
          id              bigserial PRIMARY KEY,
          token           text NOT NULL UNIQUE,
          conversation_id uuid NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
          kind            text NOT NULL,
          ciphertext      text NOT NULL,   -- Fernet; the plaintext is never stored
          created_at      timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT pii_vault_kind_ck
            CHECK (kind IN ('PAN','AADHAAR','PHONE','ACCOUNT','IFSC'))
        )
    """)
    op.execute("CREATE INDEX pii_vault_conversation_idx ON pii_vault (conversation_id)")

    # -------------------------------------------------------- consent ledger
    # Every consent event, forever. `wording` is the exact text the customer was
    # shown — "customer consented" is worthless without it, and a hash alone
    # cannot be read back to a human in a dispute.
    op.execute("""
        CREATE TABLE consent_ledger (
          id              bigserial PRIMARY KEY,
          -- Deliberately no foreign key. An append-only ledger has to outlive
          -- the row it describes: ON DELETE CASCADE would fire the trigger
          -- below and make deleting a conversation impossible, and ON DELETE
          -- NO ACTION would make it impossible for a different reason. History
          -- is not owned by the thing it is history of.
          conversation_id uuid NOT NULL,
          customer_ref    text NOT NULL,
          channel         text NOT NULL,
          event           text NOT NULL,   -- granted | refused | revoked
          wording         text NOT NULL,
          wording_hash    text NOT NULL,
          scope           jsonb NOT NULL DEFAULT '[]'::jsonb,
          source          text NOT NULL,   -- customer_reply | agent_tool | operator
          at              timestamptz NOT NULL DEFAULT now(),
          CONSTRAINT consent_event_ck CHECK (event IN ('granted','refused','revoked'))
        )
    """)
    op.execute("""
        CREATE INDEX consent_ledger_conversation_idx ON consent_ledger (conversation_id, id DESC)
    """)
    op.execute("CREATE INDEX consent_ledger_customer_idx ON consent_ledger (customer_ref, id DESC)")

    # ------------------------------------------------------------ audit log
    op.execute("""
        CREATE TABLE audit_log (
          id              bigserial PRIMARY KEY,
          conversation_id uuid,           -- no FK, for the same reason as the ledger
          event           text NOT NULL,   -- turn | tool_call | consent | followup | erasure
          stage           text,
          actor           text NOT NULL DEFAULT 'agent',
          prompt_hash     text,
          model           text,
          detail          jsonb NOT NULL DEFAULT '{}'::jsonb,
          at              timestamptz NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX audit_log_conversation_idx ON audit_log (conversation_id, id)")
    op.execute("CREATE INDEX audit_log_event_idx ON audit_log (event, at DESC)")

    # ------------------------------------------------------------- the teeth
    op.execute("""
        CREATE OR REPLACE FUNCTION refuse_mutation() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION '% is append-only: % is not permitted',
            TG_TABLE_NAME, TG_OP
            USING HINT = 'Append a correcting row instead of altering history.';
        END;
        $$ LANGUAGE plpgsql;
    """)
    for table in ("consent_ledger", "audit_log"):
        op.execute(f"""
            CREATE TRIGGER {table}_is_append_only
              BEFORE UPDATE OR DELETE ON {table}
              FOR EACH ROW EXECUTE FUNCTION refuse_mutation();
        """)


def downgrade() -> None:
    for table in ("consent_ledger", "audit_log"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_is_append_only ON {table}")
    op.execute("DROP FUNCTION IF EXISTS refuse_mutation()")
    op.execute("DROP TABLE IF EXISTS audit_log")
    op.execute("DROP TABLE IF EXISTS consent_ledger")
    op.execute("DROP TABLE IF EXISTS pii_vault")
