"""Add idempotent Sandbox human handoff requests and notification outbox."""

from alembic import op

revision = "0018_concierge_handoff_requests"
down_revision = "0017_concierge_ai_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE concierge.handoff_requests (
            id uuid PRIMARY KEY,
            session_id uuid NOT NULL REFERENCES concierge.sessions(id) ON DELETE CASCADE,
            channel text NOT NULL CHECK (channel IN ('telegram', 'simulator')),
            external_update_id bigint NOT NULL CHECK (external_update_id >= 0),
            correlation_id uuid NOT NULL,
            status text NOT NULL CHECK (status = 'requested'),
            requested_at timestamptz NOT NULL,
            UNIQUE (channel, external_update_id)
        )
    """)
    op.execute("""
        CREATE TABLE concierge.handoff_outbox (
            id uuid PRIMARY KEY,
            request_id uuid NOT NULL UNIQUE
                REFERENCES concierge.handoff_requests(id) ON DELETE CASCADE,
            target_chat_id text NOT NULL
                CHECK (length(target_chat_id) BETWEEN 1 AND 32),
            text text NOT NULL CHECK (length(text) BETWEEN 1 AND 512),
            status text NOT NULL CHECK (status IN ('pending', 'delivering', 'delivered')),
            lease_token uuid,
            lease_until timestamptz,
            created_at timestamptz NOT NULL,
            delivered_at timestamptz,
            CHECK (
                (status = 'pending' AND lease_token IS NULL
                    AND lease_until IS NULL AND delivered_at IS NULL)
                OR (status = 'delivering' AND lease_token IS NOT NULL
                    AND lease_until IS NOT NULL AND delivered_at IS NULL)
                OR (status = 'delivered' AND lease_token IS NULL
                    AND lease_until IS NULL AND delivered_at IS NOT NULL)
            )
        )
    """)
    op.execute("""
        CREATE INDEX concierge_handoff_outbox_delivery
        ON concierge.handoff_outbox (created_at)
        WHERE status IN ('pending', 'delivering')
    """)


def downgrade() -> None:
    op.execute("DROP INDEX concierge.concierge_handoff_outbox_delivery")
    op.execute("DROP TABLE concierge.handoff_outbox")
    op.execute("DROP TABLE concierge.handoff_requests")
