"""Private versioned catalog normalization and quarantine.

Revision ID: 0003_catalog_staging
"""

from alembic import op

revision = "0003_catalog_staging"
down_revision = "0002_catalog_ingestion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE data_governance.media_rights ADD COLUMN role text CHECK (role IN ('box_art'))")
    op.execute("""
        CREATE TABLE data_governance.private_media (
            run_id uuid NOT NULL REFERENCES data_governance.ingest_runs(id),
            evidence_id uuid NOT NULL,
            media_path text NOT NULL,
            content_hash char(64) NOT NULL,
            content bytea NOT NULL,
            PRIMARY KEY (run_id, evidence_id, media_path),
            FOREIGN KEY (evidence_id, media_path)
                REFERENCES data_governance.media_rights(evidence_id, media_path)
        )
    """)
    op.execute("""
        CREATE TABLE data_governance.processing_runs (
            run_id uuid NOT NULL REFERENCES data_governance.ingest_runs(id),
            rule_version text NOT NULL,
            rule_fingerprint char(64) NOT NULL,
            processed_at timestamptz NOT NULL,
            PRIMARY KEY (run_id, rule_version)
        )
    """)
    op.execute("""
        CREATE TABLE data_governance.staging_records (
            run_id uuid NOT NULL, rule_version text NOT NULL,
            evidence_id uuid NOT NULL REFERENCES data_governance.raw_evidence(id),
            source_record_id text NOT NULL, identity_key text,
            state text NOT NULL CHECK (state IN ('review', 'quarantine')),
            optional_missing text[] NOT NULL, ambiguous_identity boolean NOT NULL,
            PRIMARY KEY (run_id, rule_version, evidence_id),
            FOREIGN KEY (run_id, rule_version)
                REFERENCES data_governance.processing_runs(run_id, rule_version)
        )
    """)
    op.execute("""
        CREATE TABLE data_governance.staging_values (
            run_id uuid NOT NULL, rule_version text NOT NULL, evidence_id uuid NOT NULL,
            attribute_path text NOT NULL, normalized_value jsonb NOT NULL,
            raw_attribute_path text NOT NULL,
            PRIMARY KEY (run_id, rule_version, evidence_id, attribute_path),
            FOREIGN KEY (run_id, rule_version, evidence_id)
                REFERENCES data_governance.staging_records(run_id, rule_version, evidence_id),
            FOREIGN KEY (evidence_id, raw_attribute_path)
                REFERENCES data_governance.attribute_origins(evidence_id, attribute_path)
        )
    """)
    op.execute("""
        CREATE TABLE data_governance.staging_matches (
            run_id uuid NOT NULL, rule_version text NOT NULL,
            evidence_id uuid NOT NULL, other_evidence_id uuid NOT NULL,
            kind text NOT NULL CHECK (kind IN ('duplicate', 'conflict', 'ambiguous')),
            field text NOT NULL,
            PRIMARY KEY (run_id, rule_version, evidence_id, other_evidence_id, kind, field),
            FOREIGN KEY (run_id, rule_version, evidence_id)
                REFERENCES data_governance.staging_records(run_id, rule_version, evidence_id)
        )
    """)
    op.execute("""
        CREATE TABLE data_governance.quarantine_issues (
            run_id uuid NOT NULL, rule_version text NOT NULL, evidence_id uuid NOT NULL,
            field text NOT NULL, code text NOT NULL, cause text NOT NULL,
            PRIMARY KEY (run_id, rule_version, evidence_id, field, code),
            FOREIGN KEY (run_id, rule_version, evidence_id)
                REFERENCES data_governance.staging_records(run_id, rule_version, evidence_id)
        )
    """)
    op.execute("""
        CREATE FUNCTION data_governance.reject_staging_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'staging version is immutable'; END $$
    """)
    for table in ("private_media", "processing_runs", "staging_records", "staging_values", "staging_matches", "quarantine_issues"):
        op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON data_governance.{table} FOR EACH ROW EXECUTE FUNCTION data_governance.reject_staging_mutation()")


def downgrade() -> None:
    for table in ("quarantine_issues", "staging_matches", "staging_values", "staging_records", "processing_runs", "private_media"):
        op.execute(f"DROP TABLE data_governance.{table}")
    op.execute("DROP FUNCTION data_governance.reject_staging_mutation()")
    op.execute("ALTER TABLE data_governance.media_rights DROP COLUMN role")
