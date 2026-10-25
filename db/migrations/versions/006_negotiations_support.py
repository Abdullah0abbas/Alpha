"""add negotiations table and negotiable columns to offers

Revision ID: 006_negotiations_support
Revises: 005_v2_6_updates
Create Date: 2026-09-19 05:25:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '006_negotiations_support'
down_revision = '005_v2_6_updates'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add negotiable and negotiable_type columns to offers
    op.add_column('offers', sa.Column('negotiable', sa.Boolean(), server_default='false', nullable=False))
    op.add_column('offers', sa.Column('negotiable_type', sa.String(), server_default='none', nullable=False))
    
    # 2. Create negotiations table
    op.create_table(
        'negotiations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('offer_id', sa.Integer(), sa.ForeignKey('offers.id'), nullable=False),
        sa.Column('taker_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id'), nullable=False),
        sa.Column('maker_id', sa.BigInteger(), sa.ForeignKey('users.telegram_id'), nullable=False),
        sa.Column('proposed_rate', sa.Numeric(), nullable=False),
        sa.Column('proposed_amount', sa.Numeric(), nullable=False),
        sa.Column('status', sa.String(), server_default='PENDING_MAKER', nullable=False, index=True),
        sa.Column('round', sa.Integer(), server_default='1', nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False, index=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now())
    )


def downgrade() -> None:
    op.drop_table('negotiations')
    op.drop_column('offers', 'negotiable_type')
    op.drop_column('offers', 'negotiable')
