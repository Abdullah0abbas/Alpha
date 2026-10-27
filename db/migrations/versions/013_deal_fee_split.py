"""add seller_fee_usdt and buyer_fee_usdt to deals

Revision ID: 013_deal_fee_split
Revises: 012_offer_status_changed_at
Create Date: 2026-10-27 12:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '013_deal_fee_split'
down_revision = '012_offer_status_changed_at'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'deals',
        sa.Column('seller_fee_usdt', sa.Numeric(), server_default='0.0', nullable=False)
    )
    op.add_column(
        'deals',
        sa.Column('buyer_fee_usdt', sa.Numeric(), server_default='0.0', nullable=False)
    )
    op.alter_column('deals', 'fee_usdt', server_default='0.0')


def downgrade() -> None:
    op.alter_column('deals', 'fee_usdt', server_default='1.0')
    op.drop_column('deals', 'buyer_fee_usdt')
    op.drop_column('deals', 'seller_fee_usdt')
