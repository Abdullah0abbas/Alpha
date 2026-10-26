"""add deal_card_message_ids columns

Revision ID: 011_deal_card_message_ids
Revises: 010_add_deposit_index_seq
Create Date: 2026-10-26 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '011_deal_card_message_ids'
down_revision = '010_add_deposit_index_seq'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('deals', sa.Column('seller_card_msg_id', sa.BigInteger(), nullable=True))
    op.add_column('deals', sa.Column('buyer_card_msg_id', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('deals', 'buyer_card_msg_id')
    op.drop_column('deals', 'seller_card_msg_id')
