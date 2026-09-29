"""Create the seven module schemas.

Revision ID: 0001_module_schemas
"""

from alembic import op

revision = "0001_module_schemas"
down_revision = None
branch_labels = None
depends_on = None

SCHEMAS = ("catalog", "commerce", "rentals", "concierge", "data_governance", "quality", "platform")


def upgrade() -> None:
    for schema in SCHEMAS:
        op.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')


def downgrade() -> None:
    for schema in reversed(SCHEMAS):
        op.execute(f'DROP SCHEMA IF EXISTS "{schema}"')
