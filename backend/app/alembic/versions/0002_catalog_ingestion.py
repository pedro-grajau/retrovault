"""Preserve private catalog ingestion evidence.

Revision ID: 0002_catalog_ingestion
"""

from alembic import op

revision = "0002_catalog_ingestion"
down_revision = "0001_module_schemas"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE data_governance.ingest_runs (
            id uuid PRIMARY KEY, source text NOT NULL, source_version text NOT NULL,
            captured_at timestamptz NOT NULL, started_at timestamptz NOT NULL,
            finished_at timestamptz, state text NOT NULL CHECK (state IN ('running', 'completed')),
            package_hash char(64) NOT NULL, config_fingerprint char(64) NOT NULL,
            app_version text NOT NULL, received integer NOT NULL DEFAULT 0,
            preserved integer NOT NULL DEFAULT 0, rejected integer NOT NULL DEFAULT 0,
            pending integer NOT NULL DEFAULT 0,
            UNIQUE (source, source_version),
            CHECK (received >= 0 AND preserved >= 0 AND rejected >= 0 AND pending >= 0)
        )
    """)
    op.execute("""
        CREATE TABLE data_governance.raw_evidence (
            id uuid PRIMARY KEY, source text NOT NULL, source_record_id text NOT NULL,
            payload_hash char(64) NOT NULL, raw_payload text NOT NULL,
            captured_at timestamptz NOT NULL, preserved_at timestamptz NOT NULL,
            UNIQUE (source, source_record_id, payload_hash)
        )
    """)
    op.execute("""
        CREATE TABLE data_governance.run_evidence (
            run_id uuid NOT NULL REFERENCES data_governance.ingest_runs(id),
            evidence_id uuid NOT NULL REFERENCES data_governance.raw_evidence(id),
            PRIMARY KEY (run_id, evidence_id)
        )
    """)
    op.execute("""
        CREATE TABLE data_governance.attribute_origins (
            evidence_id uuid NOT NULL REFERENCES data_governance.raw_evidence(id),
            attribute_path text NOT NULL, source text NOT NULL, source_version text NOT NULL,
            source_record_id text NOT NULL, actor text NOT NULL, captured_at timestamptz NOT NULL,
            PRIMARY KEY (evidence_id, attribute_path)
        )
    """)
    op.execute("""
        CREATE TABLE data_governance.media_rights (
            evidence_id uuid NOT NULL REFERENCES data_governance.raw_evidence(id),
            media_path text NOT NULL, storage_right text NOT NULL CHECK (storage_right IN ('confirmed', 'denied', 'unknown')),
            publication_right text NOT NULL CHECK (publication_right IN ('confirmed', 'denied', 'unknown')),
            attribution text NOT NULL, eligible boolean NOT NULL,
            PRIMARY KEY (evidence_id, media_path),
            CHECK (NOT eligible OR (storage_right = 'confirmed' AND publication_right = 'confirmed'))
        )
    """)
    op.execute("""
        CREATE TABLE data_governance.ingest_failures (
            id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES data_governance.ingest_runs(id),
            source_record_id text NOT NULL, code text NOT NULL, correlation_id uuid NOT NULL,
            cause text NOT NULL, UNIQUE (run_id, source_record_id)
        )
    """)
    op.execute("""
        CREATE FUNCTION data_governance.reject_evidence_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN
          RAISE EXCEPTION 'raw evidence is immutable';
        END $$
    """)
    op.execute("""
        CREATE TRIGGER raw_evidence_immutable BEFORE UPDATE OR DELETE ON data_governance.raw_evidence
        FOR EACH ROW EXECUTE FUNCTION data_governance.reject_evidence_mutation()
    """)
    op.execute("""
        CREATE TRIGGER attribute_origins_immutable BEFORE UPDATE OR DELETE ON data_governance.attribute_origins
        FOR EACH ROW EXECUTE FUNCTION data_governance.reject_evidence_mutation()
    """)
    op.execute("""
        CREATE TRIGGER media_rights_immutable BEFORE UPDATE OR DELETE ON data_governance.media_rights
        FOR EACH ROW EXECUTE FUNCTION data_governance.reject_evidence_mutation()
    """)


def downgrade() -> None:
    op.execute("DROP TABLE data_governance.ingest_failures")
    op.execute("DROP TABLE data_governance.media_rights")
    op.execute("DROP TABLE data_governance.attribute_origins")
    op.execute("DROP TABLE data_governance.run_evidence")
    op.execute("DROP TABLE data_governance.raw_evidence")
    op.execute("DROP FUNCTION data_governance.reject_evidence_mutation()")
    op.execute("DROP TABLE data_governance.ingest_runs")
