"""Merge ingestion audit hardening and catalog staging revisions."""

revision = "0004_merge_catalog_heads"
down_revision = (
    "0003_immutable_ingestion_audit",
    "0003_catalog_staging",
)
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
