"""add status_changed_at to offers table

Revision ID: 012_offer_status_changed_at
Revises: 011_deal_card_message_ids
Create Date: 2026-10-26 11:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '012_offer_status_changed_at'
down_revision = '011_deal_card_message_ids'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'offers',
        sa.Column('status_changed_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=True)
    )
    op.create_check_constraint(
        'chk_deals_different_parties',
        'deals',
        'seller_id <> buyer_id'
    )
    op.create_index(
        'uq_open_deal_per_offer',
        'deals',
        ['offer_id'],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('CANCELLED','EXPIRED','COMPLETED','DISPUTED')")
    )
    op.create_index(
        'uq_open_negotiation_per_offer',
        'negotiations',
        ['offer_id'],
        unique=True,
        postgresql_where=sa.text("status IN ('PENDING_MAKER','PENDING_TAKER')")
    )


def downgrade() -> None:
    op.drop_index('uq_open_negotiation_per_offer', table_name='negotiations')
    op.drop_index('uq_open_deal_per_offer', table_name='deals')
    op.drop_constraint('chk_deals_different_parties', 'deals', type_='check')
    op.drop_column('offers', 'status_changed_at')
