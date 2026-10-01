"""Armazena evidências imutáveis de endpoints RA e métricas de progressão."""

from alembic import op

revision = "0010_ra_source_metrics"
down_revision = "0009_commerce_sandbox_offers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE data_governance.source_endpoint_responses (
            evidence_id uuid NOT NULL REFERENCES data_governance.raw_evidence(id),
            endpoint text NOT NULL,
            payload_hash char(64) NOT NULL,
            payload_bytes bytea NOT NULL,
            captured_at timestamptz NOT NULL,
            PRIMARY KEY (evidence_id, endpoint),
            CHECK (length(trim(endpoint)) > 0)
        )
    """)
    op.execute("""
        CREATE TABLE data_governance.source_metrics (
            evidence_id uuid NOT NULL REFERENCES data_governance.raw_evidence(id),
            metric_name text NOT NULL,
            metric_value jsonb NOT NULL,
            endpoint text NOT NULL,
            source text NOT NULL,
            source_version text NOT NULL,
            source_record_id text NOT NULL,
            actor text NOT NULL,
            captured_at timestamptz NOT NULL,
            PRIMARY KEY (evidence_id, metric_name),
            CHECK (jsonb_typeof(metric_value) = 'number'),
            CHECK ((metric_value::text)::numeric >= 0),
            CHECK (length(trim(metric_name)) > 0 AND length(trim(endpoint)) > 0)
        )
    """)
    for table in ("source_endpoint_responses", "source_metrics"):
        op.execute(f"""
            CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE
            ON data_governance.{table}
            FOR EACH ROW EXECUTE FUNCTION data_governance.reject_evidence_mutation()
        """)
        op.execute(f"""
            CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE
            ON data_governance.{table}
            FOR EACH STATEMENT EXECUTE FUNCTION data_governance.reject_evidence_mutation()
        """)


def downgrade() -> None:
    for table in ("source_metrics", "source_endpoint_responses"):
        op.execute(f"DROP TABLE data_governance.{table}")
