"""Allow separate idempotent budget reservations per model operation."""

from alembic import op

revision = "0019_concierge_ai_ledger_operations"
down_revision = "0018_concierge_handoff_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE concierge.ai_ledger_reservations
        ADD COLUMN operation text NOT NULL DEFAULT 'intent_extraction'
    """)
    op.execute("""
        ALTER TABLE concierge.ai_ledger_reservations
        ADD CONSTRAINT concierge_ai_ledger_operation_check
        CHECK (operation IN ('intent_extraction', 'recommendation_ranking'))
    """)
    op.execute("""
        ALTER TABLE concierge.ai_ledger_reservations
        DROP CONSTRAINT ai_ledger_reservations_channel_external_update_id_key
    """)
    op.execute("""
        ALTER TABLE concierge.ai_ledger_reservations
        ADD CONSTRAINT concierge_ai_ledger_operation_idempotency
        UNIQUE (channel, external_update_id, operation)
    """)


def downgrade() -> None:
    raise RuntimeError(
        "ai_ledger_operations_downgrade_would_discard_ranking_reservations"
    )
