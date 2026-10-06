"""Expose deterministic SKU and physical-unit facts for public discovery."""

from alembic import op

revision = "0013_commerce_sku_unit_facts"
down_revision = "0012_catalog_title_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE commerce.offers
        ADD COLUMN sku_code text NOT NULL DEFAULT ''
    """)
    op.execute("""
        UPDATE commerce.offers
        SET sku_code = 'RV-' || upper(replace(game_id::text, '-', ''))
                       || '-' || upper(substr(mode, 1, 1))
    """)
    op.execute("""
        ALTER TABLE commerce.offers
        ADD CONSTRAINT offers_sku_code_nonblank CHECK (length(trim(sku_code)) > 0),
        ADD CONSTRAINT offers_sku_code_unique UNIQUE (sku_code)
    """)
    op.execute("ALTER TABLE commerce.offers ALTER COLUMN sku_code DROP DEFAULT")
    op.execute("""
        ALTER TABLE commerce.physical_units
        ADD COLUMN condition_summary text NOT NULL DEFAULT 'Condição não informada.',
        ADD COLUMN defects jsonb,
        ADD COLUMN included_items jsonb,
        ADD CONSTRAINT physical_units_condition_summary_nonblank
            CHECK (length(trim(condition_summary)) > 0),
        ADD CONSTRAINT physical_units_defects_array
            CHECK (defects IS NULL OR jsonb_typeof(defects) = 'array'),
        ADD CONSTRAINT physical_units_defects_string_array
            CHECK (
                defects IS NULL OR (
                    jsonb_typeof(defects) = 'array'
                    AND NOT jsonb_path_exists(
                        defects, '$[*] ? (@.type() != "string")'
                    )
                )
            ),
        ADD CONSTRAINT physical_units_included_items_array
            CHECK (included_items IS NULL OR jsonb_typeof(included_items) = 'array'),
        ADD CONSTRAINT physical_units_included_items_string_array
            CHECK (
                included_items IS NULL OR (
                    jsonb_typeof(included_items) = 'array'
                    AND NOT jsonb_path_exists(
                        included_items, '$[*] ? (@.type() != "string")'
                    )
                )
            )
    """)
    op.execute("""
        UPDATE commerce.physical_units u
        SET condition_summary = o.condition_summary
        FROM commerce.offers o
        WHERE o.id = u.offer_id
    """)


def downgrade() -> None:
    op.execute("""
        ALTER TABLE commerce.physical_units
        DROP CONSTRAINT physical_units_included_items_string_array,
        DROP CONSTRAINT physical_units_included_items_array,
        DROP CONSTRAINT physical_units_defects_string_array,
        DROP CONSTRAINT physical_units_defects_array,
        DROP CONSTRAINT physical_units_condition_summary_nonblank,
        DROP COLUMN included_items,
        DROP COLUMN defects,
        DROP COLUMN condition_summary
    """)
    op.execute("""
        ALTER TABLE commerce.offers
        DROP CONSTRAINT offers_sku_code_unique,
        DROP CONSTRAINT offers_sku_code_nonblank,
        DROP COLUMN sku_code
    """)
