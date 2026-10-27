"""add payout_details to offers table

Revision ID: 014_offer_payout_details
Revises: 013_deal_fee_split
Create Date: 2026-10-27 13:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision = '014_offer_payout_details'
down_revision = '013_deal_fee_split'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'offers',
        sa.Column('payout_details', postgresql.JSONB(astext_type=sa.Text()), server_default='{}', nullable=True)
    )


def downgrade() -> None:
    op.drop_column('offers', 'payout_details')
