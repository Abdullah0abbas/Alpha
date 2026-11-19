"""add deposit_tx_hash and payout_tx_hash to deals table

Revision ID: 015_add_deal_tx_hashes
Revises: 014_offer_payout_details
Create Date: 2026-11-19 02:10:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '015_add_deal_tx_hashes'
down_revision = '014_offer_payout_details'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE deals ADD COLUMN IF NOT EXISTS deposit_tx_hash VARCHAR(128);")
    op.execute("ALTER TABLE deals ADD COLUMN IF NOT EXISTS payout_tx_hash VARCHAR(128);")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_deals_deposit_tx_hash "
        "ON deals (deposit_tx_hash) WHERE deposit_tx_hash IS NOT NULL;"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_deals_payout_tx_hash "
        "ON deals (payout_tx_hash) WHERE payout_tx_hash IS NOT NULL;"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_deals_payout_tx_hash;")
    op.execute("DROP INDEX IF EXISTS uq_deals_deposit_tx_hash;")
    op.execute("ALTER TABLE deals DROP COLUMN IF EXISTS payout_tx_hash;")
    op.execute("ALTER TABLE deals DROP COLUMN IF EXISTS deposit_tx_hash;")
