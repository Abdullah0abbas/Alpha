"""add user profile fields and reward points

Revision ID: 008_user_profile_and_rewards
Revises: 007_admin_panel
Create Date: 2026-09-23 15:50:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '008_user_profile_and_rewards'
down_revision = '007_admin_panel'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_columns = [c['name'] for c in insp.get_columns('users')]

    if 'nickname' not in existing_columns:
        op.add_column('users', sa.Column('nickname', sa.String(length=60), nullable=True))
    if 'saved_usdt_address' not in existing_columns:
        op.add_column('users', sa.Column('saved_usdt_address', sa.String(length=100), nullable=True))
    if 'saved_payment_info' not in existing_columns:
        op.add_column('users', sa.Column('saved_payment_info', sa.String(length=200), nullable=True))
    if 'kyc_status' not in existing_columns:
        op.add_column('users', sa.Column('kyc_status', sa.String(length=20), server_default='unverified', nullable=False))
    if 'reward_points' not in existing_columns:
        op.add_column('users', sa.Column('reward_points', sa.Integer(), server_default='0', nullable=False))



def downgrade() -> None:
    op.drop_column('users', 'reward_points')
    op.drop_column('users', 'kyc_status')
    op.drop_column('users', 'saved_payment_info')
    op.drop_column('users', 'saved_usdt_address')
    op.drop_column('users', 'nickname')
