"""Add the atomic monthly AI budget ledger."""

from alembic import op

revision = "0017_concierge_ai_ledger"
down_revision = "0016_concierge_reliability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE concierge.ai_ledger_months (
            period_start date PRIMARY KEY,
            posted_cost_usd numeric(14, 8) NOT NULL DEFAULT 0
                CHECK (posted_cost_usd >= 0),
            reserved_cost_usd numeric(14, 8) NOT NULL DEFAULT 0
                CHECK (reserved_cost_usd >= 0),
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE concierge.ai_ledger_reservations (
            id uuid PRIMARY KEY,
            session_id uuid NOT NULL,
            channel text NOT NULL CHECK (channel IN ('telegram', 'simulator')),
            external_update_id bigint NOT NULL CHECK (external_update_id >= 0),
            period_start date NOT NULL
                REFERENCES concierge.ai_ledger_months(period_start),
            correlation_id uuid NOT NULL,
            model_snapshot text NOT NULL CHECK (length(model_snapshot) BETWEEN 1 AND 128),
            prompt_version text NOT NULL CHECK (length(prompt_version) BETWEEN 1 AND 64),
            workflow_version text NOT NULL CHECK (length(workflow_version) BETWEEN 1 AND 64),
            configuration_version text NOT NULL CHECK (length(configuration_version) BETWEEN 1 AND 64),
            input_token_limit integer NOT NULL CHECK (input_token_limit > 0),
            output_token_limit integer NOT NULL CHECK (output_token_limit > 0),
            reserved_cost_usd numeric(14, 8) NOT NULL CHECK (reserved_cost_usd > 0),
            actual_input_tokens integer,
            actual_output_tokens integer,
            actual_cost_usd numeric(14, 8),
            status text NOT NULL CHECK (status IN ('reserved', 'posted', 'released', 'expired')),
            created_at timestamptz NOT NULL,
            updated_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            UNIQUE (channel, external_update_id),
            CHECK (
                (actual_input_tokens IS NULL AND actual_output_tokens IS NULL
                    AND actual_cost_usd IS NULL)
                OR (actual_input_tokens >= 0 AND actual_output_tokens >= 0
                    AND actual_cost_usd >= 0)
            ),
            CHECK (expires_at > created_at)
        )
    """)
    op.execute("""
        CREATE INDEX concierge_ai_ledger_orphan_expiry
        ON concierge.ai_ledger_reservations (period_start, expires_at, id)
        WHERE status = 'reserved'
    """)
    op.execute("""
        CREATE INDEX concierge_ai_ledger_retention
        ON concierge.ai_ledger_reservations (created_at)
        WHERE status IN ('posted', 'released', 'expired')
    """)


def downgrade() -> None:
    op.execute("DROP INDEX concierge.concierge_ai_ledger_retention")
    op.execute("DROP INDEX concierge.concierge_ai_ledger_orphan_expiry")
    op.execute("DROP TABLE concierge.ai_ledger_reservations")
    op.execute("DROP TABLE concierge.ai_ledger_months")
