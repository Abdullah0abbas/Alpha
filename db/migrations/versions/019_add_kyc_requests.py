"""add kyc_requests table for optional user identity verification

Revision ID: 019_add_kyc_requests
Revises: 018_add_offer_note_duration
Create Date: 2026-11-23 04:20:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '019_add_kyc_requests'
down_revision = '018_add_offer_note_duration'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE IF NOT EXISTS kyc_requests (
            id SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(telegram_id),
            full_name VARCHAR(120) NOT NULL,
            national_id VARCHAR(40) NOT NULL,
            phone_number VARCHAR(40) NOT NULL,
            id_front_file_id VARCHAR(255) NOT NULL,
            id_back_file_id VARCHAR(255) NOT NULL,
            selfie_file_id VARCHAR(255) NOT NULL,
            status VARCHAR(20) DEFAULT 'pending',
            rejection_reason VARCHAR(300),
            reviewed_by BIGINT,
            reviewed_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now()
        );
    """)
    op.execute("CREATE INDEX IF NOT EXISTS ix_kyc_requests_user_id ON kyc_requests (user_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_kyc_requests_status ON kyc_requests (status);")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS kyc_requests;")
