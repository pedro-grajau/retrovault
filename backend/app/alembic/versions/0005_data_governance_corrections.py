"""Preserve exact source bytes and append-only human corrections."""

from alembic import op

revision = "0005_data_governance_corrections"
down_revision = "0004_merge_catalog_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DROP TRIGGER raw_evidence_immutable ON data_governance.raw_evidence")
    op.execute("ALTER TABLE data_governance.raw_evidence ADD COLUMN raw_payload_bytes bytea")
    op.execute("UPDATE data_governance.raw_evidence SET raw_payload_bytes = convert_to(raw_payload, 'UTF8')")
    op.execute("ALTER TABLE data_governance.raw_evidence ALTER COLUMN raw_payload_bytes SET NOT NULL")
    op.execute("""
        CREATE TRIGGER raw_evidence_immutable BEFORE UPDATE OR DELETE ON data_governance.raw_evidence
        FOR EACH ROW EXECUTE FUNCTION data_governance.reject_evidence_mutation()
    """)
    op.execute("""
        CREATE TABLE data_governance.review_corrections (
            id uuid PRIMARY KEY,
            run_id uuid NOT NULL,
            rule_version text NOT NULL,
            evidence_id uuid NOT NULL,
            revision integer NOT NULL CHECK (revision > 0),
            field text NOT NULL CHECK (field IN (
                'title', 'platform', 'region', 'edition', 'genre', 'developer',
                'publisher', 'year', 'rating', 'description', 'included_items'
            )),
            value jsonb NOT NULL,
            previous_value jsonb NOT NULL,
            actor text NOT NULL CHECK (actor = 'Eduardo'),
            reason text NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 1000),
            previous_etag text NOT NULL,
            resulting_etag text NOT NULL,
            corrected_at timestamptz NOT NULL,
            UNIQUE (run_id, rule_version, evidence_id, revision),
            FOREIGN KEY (run_id, rule_version, evidence_id)
                REFERENCES data_governance.staging_records(run_id, rule_version, evidence_id)
        )
    """)
    op.execute("""
        CREATE TRIGGER review_corrections_immutable BEFORE UPDATE OR DELETE
        ON data_governance.review_corrections
        FOR EACH ROW EXECUTE FUNCTION data_governance.reject_staging_mutation()
    """)
    op.execute("""
        CREATE TRIGGER review_corrections_no_truncate BEFORE TRUNCATE
        ON data_governance.review_corrections
        FOR EACH STATEMENT EXECUTE FUNCTION data_governance.reject_staging_mutation()
    """)


def downgrade() -> None:
    op.execute("DROP TABLE data_governance.review_corrections")
    op.execute("DROP TRIGGER raw_evidence_immutable ON data_governance.raw_evidence")
    op.execute("ALTER TABLE data_governance.raw_evidence DROP COLUMN raw_payload_bytes")
    op.execute("""
        CREATE TRIGGER raw_evidence_immutable BEFORE UPDATE OR DELETE ON data_governance.raw_evidence
        FOR EACH ROW EXECUTE FUNCTION data_governance.reject_evidence_mutation()
    """)
