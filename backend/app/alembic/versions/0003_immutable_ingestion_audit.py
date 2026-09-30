"""Protect run links and ingestion failures from mutation."""

from alembic import op

revision = "0003_immutable_ingestion_audit"
down_revision = "0002_catalog_ingestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TRIGGER run_evidence_immutable BEFORE UPDATE OR DELETE
        ON data_governance.run_evidence
        FOR EACH ROW EXECUTE FUNCTION data_governance.reject_evidence_mutation()
    """)
    op.execute("""
        CREATE TRIGGER ingest_failures_immutable BEFORE UPDATE OR DELETE
        ON data_governance.ingest_failures
        FOR EACH ROW EXECUTE FUNCTION data_governance.reject_evidence_mutation()
    """)
    for table in (
        "raw_evidence",
        "attribute_origins",
        "media_rights",
        "run_evidence",
        "ingest_failures",
    ):
        op.execute(f"""
            CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE
            ON data_governance.{table}
            FOR EACH STATEMENT EXECUTE FUNCTION data_governance.reject_evidence_mutation()
        """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER ingest_failures_immutable ON data_governance.ingest_failures"
    )
    op.execute(
        "DROP TRIGGER run_evidence_immutable ON data_governance.run_evidence"
    )
    for table in (
        "raw_evidence",
        "attribute_origins",
        "media_rights",
        "run_evidence",
        "ingest_failures",
    ):
        op.execute(f"DROP TRIGGER {table}_no_truncate ON data_governance.{table}")
