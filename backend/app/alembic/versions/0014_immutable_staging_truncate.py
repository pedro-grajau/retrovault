"""Prevent truncating versioned Catalog staging and private media tables.

Revision ID: 0014_immutable_staging_truncate
"""

from alembic import op

revision = "0014_immutable_staging_truncate"
down_revision = "0013_commerce_sku_unit_facts"
branch_labels = None
depends_on = None

_TABLES = (
    "private_media",
    "processing_runs",
    "staging_records",
    "staging_values",
    "staging_matches",
    "quarantine_issues",
)


def upgrade() -> None:
    for table in _TABLES:
        op.execute(f"""
            CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE
            ON data_governance.{table}
            FOR EACH STATEMENT EXECUTE FUNCTION data_governance.reject_staging_mutation()
        """)


def downgrade() -> None:
    for table in _TABLES:
        op.execute(
            f"DROP TRIGGER {table}_no_truncate ON data_governance.{table}"
        )
