"""Allow corrections to preserve RetroAchievements release-date precision."""

from alembic import op

revision = "0008_release_date_precision"
down_revision = "0007_ingest_manifest_fingerprint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE data_governance.review_corrections "
        "DROP CONSTRAINT review_corrections_field_check"
    )
    op.execute("""
        ALTER TABLE data_governance.review_corrections
        ADD CONSTRAINT review_corrections_field_check CHECK (field IN (
            'title', 'platform', 'region', 'edition', 'genre', 'developer',
            'publisher', 'year', 'release_date', 'release_date_granularity',
            'rating', 'description', 'included_items'
        ))
    """)


def downgrade() -> None:
    op.execute(
        "ALTER TABLE data_governance.review_corrections "
        "DROP CONSTRAINT review_corrections_field_check"
    )
    op.execute("""
        ALTER TABLE data_governance.review_corrections
        ADD CONSTRAINT review_corrections_field_check CHECK (field IN (
            'title', 'platform', 'region', 'edition', 'genre', 'developer',
            'publisher', 'year', 'rating', 'description', 'included_items'
        ))
    """)
