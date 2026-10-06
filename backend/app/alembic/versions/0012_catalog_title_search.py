"""Add indexed, accent-insensitive title search for published games."""

from alembic import op

revision = "0012_catalog_title_search"
down_revision = "0011_private_use_cover_decisions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm WITH SCHEMA public")
    # Supabase keeps most extensions in `extensions`; local PostgreSQL commonly
    # installs them in `public`. Resolve both without relocating an existing extension.
    op.execute("SET LOCAL search_path TO pg_catalog, extensions, public")
    op.execute("""
        CREATE FUNCTION catalog.normalize_title(value text) RETURNS text
        LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
        SET search_path TO pg_catalog, extensions, public
        AS $$
            SELECT trim(regexp_replace(
                unaccent(lower(value)),
                '[^[:alnum:]]+', ' ', 'g'
            ))
        $$
    """)
    op.execute("""
        CREATE INDEX published_games_title_search_trgm_idx
        ON catalog.published_games USING gin
            (catalog.normalize_title(title) gin_trgm_ops)
        WHERE active
    """)
    op.execute("""
        CREATE INDEX published_games_title_search_fts_idx
        ON catalog.published_games USING gin
            (to_tsvector('simple', catalog.normalize_title(title)))
        WHERE active
    """)


def downgrade() -> None:
    op.execute("DROP INDEX catalog.published_games_title_search_fts_idx")
    op.execute("DROP INDEX catalog.published_games_title_search_trgm_idx")
    op.execute("DROP FUNCTION catalog.normalize_title(text)")
