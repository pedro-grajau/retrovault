"""Store manifest fingerprints for completed source-run cache hits."""

from alembic import op

revision = "0007_ingest_manifest_fingerprint"
down_revision = "0006_catalog_publication"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE data_governance.ingest_runs "
        "ADD COLUMN manifest_hash char(64)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE data_governance.ingest_runs DROP COLUMN manifest_hash"
    )
