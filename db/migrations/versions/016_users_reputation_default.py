"""set users.reputation_score server_default to 100

Revision ID: 016_users_reputation_default
Revises: 015_add_deal_tx_hashes
Create Date: 2026-11-19 02:35:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '016_users_reputation_default'
down_revision = '015_add_deal_tx_hashes'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ALTER COLUMN reputation_score SET DEFAULT 100;")


def downgrade() -> None:
    op.execute("ALTER TABLE users ALTER COLUMN reputation_score SET DEFAULT 0;")
