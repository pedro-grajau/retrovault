"""Persist only validated recommendation evidence with its delivery outbox."""

from alembic import op

revision = "0020_concierge_outbox_recommendation_context"
down_revision = "0019_concierge_ai_ledger_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE concierge.outbox
        ADD COLUMN recommendation_context jsonb
    """)
    op.execute("""
        ALTER TABLE concierge.outbox
        ADD CONSTRAINT concierge_outbox_recommendation_context_object
        CHECK (
            recommendation_context IS NULL
            OR jsonb_typeof(recommendation_context) = 'object'
        )
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE concierge.outbox
        DROP CONSTRAINT concierge_outbox_recommendation_context_object
    """)
    op.execute("""
        ALTER TABLE concierge.outbox DROP COLUMN recommendation_context
    """)
