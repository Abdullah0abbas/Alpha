"""add note and duration_hours columns to offers table

Revision ID: 018_add_offer_note_duration
Revises: 017_sync_schema_and_indexes
Create Date: 2026-11-21 14:33:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '018_add_offer_note_duration'
down_revision = '017_sync_schema_and_indexes'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS note VARCHAR(60);")
    op.execute("ALTER TABLE offers ADD COLUMN IF NOT EXISTS duration_hours NUMERIC DEFAULT 4.0;")


def downgrade() -> None:
    op.execute("ALTER TABLE offers DROP COLUMN IF EXISTS duration_hours;")
    op.execute("ALTER TABLE offers DROP COLUMN IF EXISTS note;")
