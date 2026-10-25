"""add payout_info to offers

Revision ID: 004_add_payout_info_to_offers
Revises: 003_add_expires_at_to_deals
Create Date: 2026-09-16 15:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '004_add_payout_info_to_offers'
down_revision: Union[str, None] = '003_add_expires_at_to_deals'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('offers', sa.Column('payout_info', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('offers', 'payout_info')
