"""add expires_at to deals

Revision ID: 003_add_expires_at_to_deals
Revises: 002_add_channel_message_id
Create Date: 2026-09-16 15:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '003_add_expires_at_to_deals'
down_revision: Union[str, None] = '002_add_channel_message_id'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('deals', sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True))
    op.create_index(op.f('ix_deals_expires_at'), 'deals', ['expires_at'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_deals_expires_at'), table_name='deals')
    op.drop_column('deals', 'expires_at')
