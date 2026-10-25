"""add system_settings table

Revision ID: 007_admin_panel
Revises: 006_negotiations_support
Create Date: 2026-09-22 05:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision = '007_admin_panel'
down_revision = '006_negotiations_support'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Create system_settings table
    op.create_table(
        'system_settings',
        sa.Column('key', sa.String(), primary_key=True),
        sa.Column('value', JSONB(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now())
    )


def downgrade() -> None:
    op.drop_table('system_settings')
