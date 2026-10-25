"""add audit_logs table

Revision ID: 009_audit_logs
Revises: 008_user_profile_and_rewards
Create Date: 2026-09-23 16:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '009_audit_logs'
down_revision = '008_user_profile_and_rewards'
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    existing_tables = insp.get_table_names()

    if 'audit_logs' not in existing_tables:
        op.create_table(
            'audit_logs',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column('actor_id', sa.BigInteger(), nullable=True, index=True),
            sa.Column('actor_type', sa.String(length=30), server_default='ADMIN', nullable=False, index=True),
            sa.Column('action', sa.String(length=50), nullable=False, index=True),
            sa.Column('target_type', sa.String(length=30), nullable=False, index=True),
            sa.Column('target_id', sa.String(length=100), nullable=False, index=True),
            sa.Column('details', JSONB(), nullable=True),
            sa.Column('ip_or_source', sa.String(length=50), server_default='TELEGRAM_BOT', nullable=True),
            sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False, index=True)
        )



def downgrade() -> None:
    op.drop_table('audit_logs')
