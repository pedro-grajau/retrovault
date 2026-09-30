"""Publish reviewed catalog projections and transactional outbox events."""

from alembic import op

revision = "0006_catalog_publication"
down_revision = "0005_data_governance_corrections"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE catalog.published_media (
            content_hash char(64) PRIMARY KEY,
            content bytea NOT NULL,
            content_type text NOT NULL CHECK (content_type IN ('image/png', 'image/jpeg', 'image/webp')),
            attribution text NOT NULL,
            rights_reference text NOT NULL,
            created_at timestamptz NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE catalog.published_games (
            id uuid PRIMARY KEY,
            source text NOT NULL,
            source_record_id text NOT NULL,
            title text NOT NULL,
            platform text NOT NULL,
            editorial jsonb NOT NULL,
            version integer NOT NULL CHECK (version > 0),
            etag text NOT NULL,
            active boolean NOT NULL DEFAULT true,
            cover_hash char(64) NOT NULL REFERENCES catalog.published_media(content_hash),
            updated_at timestamptz NOT NULL,
            UNIQUE (source, source_record_id),
            CHECK (length(trim(title)) > 0 AND length(trim(platform)) > 0)
        )
    """)
    op.execute("""
        CREATE TABLE catalog.published_game_versions (
            game_id uuid NOT NULL,
            version integer NOT NULL,
            action text NOT NULL CHECK (action IN ('publish', 'update', 'retire')),
            snapshot jsonb NOT NULL,
            etag text NOT NULL,
            actor text NOT NULL CHECK (actor = 'Eduardo'),
            reason text NOT NULL CHECK (length(trim(reason)) BETWEEN 1 AND 1000),
            source text NOT NULL,
            source_record_id text NOT NULL,
            created_at timestamptz NOT NULL,
            PRIMARY KEY (game_id, version),
            FOREIGN KEY (game_id) REFERENCES catalog.published_games(id)
        )
    """)
    op.execute("""
        CREATE TABLE catalog.publication_audit (
            id uuid PRIMARY KEY,
            game_id uuid NOT NULL,
            version integer NOT NULL,
            action text NOT NULL CHECK (action IN ('publish', 'update', 'retire')),
            actor text NOT NULL CHECK (actor = 'Eduardo'),
            reason text NOT NULL,
            source text NOT NULL,
            source_record_id text NOT NULL,
            correlation_id uuid NOT NULL,
            occurred_at timestamptz NOT NULL,
            FOREIGN KEY (game_id, version)
                REFERENCES catalog.published_game_versions(game_id, version)
        )
    """)
    op.execute("""
        CREATE TABLE catalog.command_idempotency (
            id uuid PRIMARY KEY,
            command text NOT NULL CHECK (command IN ('publish', 'retire')),
            idempotency_key text NOT NULL CHECK (length(idempotency_key) BETWEEN 1 AND 200),
            request_hash char(64) NOT NULL,
            response jsonb NOT NULL,
            created_at timestamptz NOT NULL,
            UNIQUE (idempotency_key)
        )
    """)
    op.execute("""
        CREATE TABLE platform.outbox_events (
            id uuid PRIMARY KEY,
            topic text NOT NULL,
            aggregate_type text NOT NULL,
            aggregate_id uuid NOT NULL,
            payload jsonb NOT NULL,
            created_at timestamptz NOT NULL,
            available_at timestamptz NOT NULL,
            attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            delivered_at timestamptz
        )
    """)
    op.execute("CREATE INDEX outbox_events_pending_idx ON platform.outbox_events (available_at, created_at) WHERE delivered_at IS NULL")
    op.execute("""
        CREATE FUNCTION catalog.reject_publication_audit_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'catalog publication history is immutable'; END $$
    """)
    for table in ("published_media", "published_game_versions", "publication_audit", "command_idempotency"):
        op.execute(f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON catalog.{table} FOR EACH ROW EXECUTE FUNCTION catalog.reject_publication_audit_mutation()")
        op.execute(f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON catalog.{table} FOR EACH STATEMENT EXECUTE FUNCTION catalog.reject_publication_audit_mutation()")


def downgrade() -> None:
    for table in ("command_idempotency", "publication_audit", "published_game_versions"):
        op.execute(f"DROP TABLE catalog.{table}")
    op.execute("DROP TABLE catalog.published_games")
    op.execute("DROP TABLE catalog.published_media")
    op.execute("DROP TABLE platform.outbox_events")
    op.execute("DROP FUNCTION catalog.reject_publication_audit_mutation()")
