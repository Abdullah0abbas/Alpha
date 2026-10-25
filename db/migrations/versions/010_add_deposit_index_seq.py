"""add deposit_index_seq sequence

Revision ID: 010_add_deposit_index_seq
Revises: 009_audit_logs
Create Date: 2026-09-23 17:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '010_add_deposit_index_seq'
down_revision = '009_audit_logs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE IF NOT EXISTS deposit_index_seq")


def downgrade() -> None:
    op.execute("DROP SEQUENCE IF EXISTS deposit_index_seq")
