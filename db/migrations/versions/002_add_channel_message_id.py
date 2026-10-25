"""add channel_message_id to offers

Revision ID: 002_add_channel_message_id
Revises: 001_initial
Create Date: 2026-08-18 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '002_add_channel_message_id'
down_revision: Union[str, None] = '001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('offers', sa.Column('channel_message_id', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('offers', 'channel_message_id')
