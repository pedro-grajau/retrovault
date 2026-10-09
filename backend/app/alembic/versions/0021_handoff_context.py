"""Persist minimized structured recommendation context on human handoffs."""

from alembic import op

revision = "0021_handoff_context"
down_revision = "0020_outbox_rec_context"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE concierge.handoff_requests
        ADD COLUMN context_snapshot jsonb
    """)
    op.execute("""
        ALTER TABLE concierge.handoff_requests
        ADD CONSTRAINT concierge_handoff_context_snapshot_object
        CHECK (
            context_snapshot IS NULL
            OR jsonb_typeof(context_snapshot) = 'object'
        )
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE concierge.handoff_requests
        DROP CONSTRAINT concierge_handoff_context_snapshot_object
    """)
    op.execute("""
        ALTER TABLE concierge.handoff_requests
        DROP COLUMN context_snapshot
    """)
