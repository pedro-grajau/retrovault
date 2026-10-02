"""Record project-scoped private-use decisions for catalog covers."""

from alembic import op

revision = "0011_private_use_cover_decisions"
down_revision = "0010_ra_source_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE data_governance.private_use_cover_decisions (
            id uuid PRIMARY KEY,
            evidence_id uuid NOT NULL,
            media_path text NOT NULL,
            scope text NOT NULL CHECK (scope = 'loopback_only'),
            basis text NOT NULL CHECK (length(trim(basis)) BETWEEN 1 AND 1000),
            terms_reference text NOT NULL CHECK (length(trim(terms_reference)) BETWEEN 1 AND 500),
            actor text NOT NULL CHECK (length(trim(actor)) BETWEEN 1 AND 100),
            decided_at timestamptz NOT NULL,
            FOREIGN KEY (evidence_id, media_path)
                REFERENCES data_governance.media_rights(evidence_id, media_path),
            UNIQUE (evidence_id, media_path, scope, basis, terms_reference, actor)
        )
    """)
    op.execute("""
        CREATE INDEX private_use_cover_decisions_latest_idx
        ON data_governance.private_use_cover_decisions (evidence_id, media_path, decided_at DESC)
    """)
    op.execute("""
        CREATE TRIGGER private_use_cover_decisions_immutable BEFORE UPDATE OR DELETE
        ON data_governance.private_use_cover_decisions
        FOR EACH ROW EXECUTE FUNCTION data_governance.reject_evidence_mutation()
    """)
    op.execute("""
        CREATE TRIGGER private_use_cover_decisions_no_truncate BEFORE TRUNCATE
        ON data_governance.private_use_cover_decisions
        FOR EACH STATEMENT EXECUTE FUNCTION data_governance.reject_evidence_mutation()
    """)


def downgrade() -> None:
    op.execute("DROP TABLE data_governance.private_use_cover_decisions")
