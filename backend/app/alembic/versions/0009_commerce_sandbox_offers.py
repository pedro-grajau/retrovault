"""Add the minimal Commerce read model and Sandbox offer fixtures."""

from alembic import op

revision = "0009_commerce_sandbox_offers"
down_revision = "0008_release_date_precision"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE commerce.offers (
            id uuid PRIMARY KEY,
            game_id uuid NOT NULL,
            mode text NOT NULL CHECK (mode IN ('purchase', 'rental')),
            price_minor integer NOT NULL CHECK (price_minor >= 0),
            currency char(3) NOT NULL CHECK (currency = 'BRL'),
            condition_summary text NOT NULL CHECK (length(trim(condition_summary)) > 0),
            demo_rank integer NOT NULL CHECK (demo_rank > 0),
            sandbox boolean NOT NULL DEFAULT true CHECK (sandbox),
            created_at timestamptz NOT NULL,
            UNIQUE (game_id, mode)
        )
    """)
    op.execute("""
        CREATE TABLE commerce.physical_units (
            id uuid PRIMARY KEY,
            offer_id uuid NOT NULL REFERENCES commerce.offers(id),
            state text NOT NULL CHECK (state IN ('available', 'unavailable')),
            created_at timestamptz NOT NULL
        )
    """)
    op.execute("CREATE INDEX offers_game_demo_rank_idx ON commerce.offers (game_id, demo_rank)")
    op.execute("CREATE INDEX physical_units_offer_state_idx ON commerce.physical_units (offer_id, state)")


def downgrade() -> None:
    op.execute("DROP TABLE commerce.physical_units")
    op.execute("DROP TABLE commerce.offers")
