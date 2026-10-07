"""Add fenced processing, one-use context references, and recoverable delivery."""

from alembic import op

revision = "0016_concierge_reliability"
down_revision = "0015_concierge_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Give existing processing rows a lease identity so the new schema can
    # recover work safely, including rows created during a rolling upgrade.
    op.execute("ALTER TABLE concierge.messages ADD COLUMN IF NOT EXISTS lease_token uuid")
    op.execute("""
        UPDATE concierge.messages
        SET lease_token = gen_random_uuid(), lease_until = COALESCE(lease_until, now())
        WHERE status = 'processing'
    """)
    op.execute("""
        ALTER TABLE concierge.messages DROP CONSTRAINT IF EXISTS messages_check
    """)
    op.execute("""
        ALTER TABLE concierge.messages
        ADD CONSTRAINT concierge_messages_lease_consistency CHECK (
            (status = 'processing' AND lease_token IS NOT NULL AND lease_until IS NOT NULL)
            OR (status <> 'processing' AND lease_token IS NULL AND lease_until IS NULL)
        )
    """)

    op.execute("""
        CREATE TABLE IF NOT EXISTS concierge.message_replay_anomalies (
            id uuid PRIMARY KEY,
            source_message_id uuid NOT NULL
                REFERENCES concierge.messages(id) ON DELETE CASCADE,
            incoming_external_user_hash text NOT NULL
                CHECK (length(incoming_external_user_hash) = 64),
            incoming_external_chat_hash text NOT NULL
                CHECK (length(incoming_external_chat_hash) = 64),
            incoming_message_id bigint NOT NULL CHECK (incoming_message_id > 0),
            received_at timestamptz NOT NULL,
            correlation_id uuid NOT NULL,
            UNIQUE (
                source_message_id,
                incoming_external_user_hash,
                incoming_external_chat_hash,
                incoming_message_id
            )
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS concierge.context_reference_uses (
            token_hash text PRIMARY KEY CHECK (length(token_hash) = 64),
            session_id uuid NOT NULL REFERENCES concierge.sessions(id) ON DELETE CASCADE,
            channel text NOT NULL CHECK (channel IN ('telegram', 'simulator')),
            external_update_id bigint NOT NULL CHECK (external_update_id >= 0),
            consumed_at timestamptz NOT NULL,
            UNIQUE (channel, external_update_id)
        )
    """)

    op.execute("ALTER TABLE concierge.outbox ADD COLUMN IF NOT EXISTS lease_token uuid")
    op.execute("ALTER TABLE concierge.outbox ADD COLUMN IF NOT EXISTS lease_until timestamptz")
    op.execute("ALTER TABLE concierge.outbox DROP CONSTRAINT IF EXISTS outbox_status_check")
    op.execute("ALTER TABLE concierge.outbox DROP CONSTRAINT IF EXISTS outbox_check")
    op.execute("DROP INDEX IF EXISTS concierge.concierge_outbox_pending")
    op.execute("DROP INDEX IF EXISTS concierge.concierge_outbox_delivery")
    op.execute("""
        ALTER TABLE concierge.outbox
        ADD CONSTRAINT outbox_status_check
        CHECK (status IN ('pending', 'delivering', 'delivered'))
    """)
    op.execute("""
        ALTER TABLE concierge.outbox
        ADD CONSTRAINT concierge_outbox_lease_consistency CHECK (
            (status = 'pending' AND lease_token IS NULL
                AND lease_until IS NULL AND delivered_at IS NULL)
            OR (status = 'delivering' AND lease_token IS NOT NULL
                AND lease_until IS NOT NULL AND delivered_at IS NULL)
            OR (status = 'delivered' AND lease_token IS NULL
                AND lease_until IS NULL AND delivered_at IS NOT NULL)
        )
    """)
    op.execute("""
        CREATE INDEX concierge_outbox_delivery
        ON concierge.outbox (created_at)
        WHERE status IN ('pending', 'delivering')
    """)


def downgrade() -> None:
    # A downgrade returns in-flight deliveries to the old retryable state.
    op.execute("""
        UPDATE concierge.outbox
        SET status = 'pending', lease_token = NULL, lease_until = NULL
        WHERE status = 'delivering'
    """)
    op.execute("DROP INDEX IF EXISTS concierge.concierge_outbox_delivery")
    op.execute("""
        CREATE INDEX concierge_outbox_pending
        ON concierge.outbox (created_at)
        WHERE status = 'pending'
    """)
    op.execute("ALTER TABLE concierge.outbox DROP CONSTRAINT concierge_outbox_lease_consistency")
    op.execute("ALTER TABLE concierge.outbox DROP CONSTRAINT outbox_status_check")
    op.execute("""
        ALTER TABLE concierge.outbox
        ADD CONSTRAINT outbox_status_check CHECK (status IN ('pending', 'delivered'))
    """)
    op.execute("""
        ALTER TABLE concierge.outbox
        ADD CONSTRAINT outbox_check CHECK (
            (status = 'pending' AND delivered_at IS NULL)
            OR (status = 'delivered' AND delivered_at IS NOT NULL)
        )
    """)
    op.execute("ALTER TABLE concierge.outbox DROP COLUMN lease_until")
    op.execute("ALTER TABLE concierge.outbox DROP COLUMN lease_token")
    op.execute("DROP TABLE concierge.context_reference_uses")
    op.execute("DROP TABLE concierge.message_replay_anomalies")
    op.execute("ALTER TABLE concierge.messages DROP CONSTRAINT concierge_messages_lease_consistency")
    op.execute("ALTER TABLE concierge.messages DROP COLUMN lease_token")
