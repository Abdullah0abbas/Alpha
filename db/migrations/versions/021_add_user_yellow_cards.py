"""add yellow_cards column to users table for warning tracking

Revision ID: 021_add_user_yellow_cards
Revises: 020_upgrade_bc_tx
Create Date: 2026-11-27 03:15:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "021_add_user_yellow_cards"
down_revision = "020_upgrade_bc_tx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS yellow_cards INTEGER NOT NULL DEFAULT 0;"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE users DROP COLUMN IF EXISTS yellow_cards;"
    )
