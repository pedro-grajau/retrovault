"""Create durable, deduplicated Concierge conversations and delivery outbox."""

from alembic import op

revision = "0015_concierge_sessions"
down_revision = "0014_immutable_staging_truncate"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE concierge.sessions (
            id uuid PRIMARY KEY,
            channel text NOT NULL CHECK (channel IN ('telegram', 'simulator')),
            external_user_id text NOT NULL CHECK (length(external_user_id) BETWEEN 1 AND 128),
            external_chat_id text NOT NULL CHECK (length(external_chat_id) BETWEEN 1 AND 128),
            context_game_id uuid,
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'terminal')),
            workflow_version text NOT NULL CHECK (length(workflow_version) BETWEEN 1 AND 64),
            correlation_id uuid NOT NULL,
            started_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            last_message_at timestamptz NOT NULL,
            last_message_id bigint NOT NULL CHECK (last_message_id > 0)
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX concierge_one_active_session_per_sender
        ON concierge.sessions (channel, external_user_id)
        WHERE status = 'active'
    """)
    op.execute("""
        CREATE INDEX concierge_sessions_retention
        ON concierge.sessions (updated_at)
    """)
    op.execute("""
        CREATE TABLE concierge.messages (
            id uuid PRIMARY KEY,
            session_id uuid REFERENCES concierge.sessions(id) ON DELETE CASCADE,
            channel text NOT NULL CHECK (channel IN ('telegram', 'simulator')),
            external_update_id bigint NOT NULL CHECK (external_update_id >= 0),
            external_message_id bigint NOT NULL CHECK (external_message_id > 0),
            external_user_id text NOT NULL CHECK (length(external_user_id) BETWEEN 1 AND 128),
            external_chat_id text NOT NULL CHECK (length(external_chat_id) BETWEEN 1 AND 128),
            text text NOT NULL CHECK (length(text) BETWEEN 1 AND 4096),
            sent_at timestamptz NOT NULL,
            received_at timestamptz NOT NULL,
            status text NOT NULL CHECK (status IN ('processing', 'reconciliation', 'processed')),
            lease_until timestamptz,
            correlation_id uuid NOT NULL,
            UNIQUE (channel, external_update_id)
        )
    """)
    op.execute("""
        CREATE INDEX concierge_messages_retention
        ON concierge.messages (received_at)
    """)
    op.execute("""
        CREATE INDEX concierge_messages_reconciliation
        ON concierge.messages (status, sent_at)
        WHERE status = 'reconciliation'
    """)
    op.execute("""
        CREATE TABLE concierge.outbox (
            id uuid PRIMARY KEY,
            session_id uuid NOT NULL REFERENCES concierge.sessions(id) ON DELETE CASCADE,
            source_message_id uuid NOT NULL UNIQUE
                REFERENCES concierge.messages(id) ON DELETE CASCADE,
            channel text NOT NULL CHECK (channel IN ('telegram', 'simulator')),
            external_update_id bigint NOT NULL CHECK (external_update_id >= 0),
            external_chat_id text NOT NULL CHECK (length(external_chat_id) BETWEEN 1 AND 128),
            text text NOT NULL CHECK (length(text) BETWEEN 1 AND 4096),
            status text NOT NULL CHECK (status IN ('pending', 'delivered')),
            created_at timestamptz NOT NULL,
            delivered_at timestamptz,
            UNIQUE (channel, external_update_id),
            CHECK ((status = 'pending' AND delivered_at IS NULL)
                OR (status = 'delivered' AND delivered_at IS NOT NULL))
        )
    """)
    op.execute("""
        CREATE INDEX concierge_outbox_pending
        ON concierge.outbox (created_at)
        WHERE status = 'pending'
    """)


def downgrade() -> None:
    op.execute("DROP TABLE concierge.outbox")
    op.execute("DROP TABLE concierge.messages")
    op.execute("DROP TABLE concierge.sessions")
