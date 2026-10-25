"""add expires_at to offers and update deals default status

Revision ID: 005_v2_6_updates
Revises: 004_add_payout_info_to_offers
Create Date: 2026-09-19 03:45:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '005_v2_6_updates'
down_revision = '004_add_payout_info_to_offers'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add expires_at to offers
    op.add_column('offers', sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))
    
    # 2. Update default status for deals to PENDING_OWNER_APPROVAL
    op.alter_column('deals', 'status', server_default='PENDING_OWNER_APPROVAL')


def downgrade() -> None:
    op.alter_column('deals', 'status', server_default='CREATED')
    op.drop_column('offers', 'expires_at')
