"""sync deal_ratings table and partial unique indexes with models

Revision ID: 017_sync_schema_and_indexes
Revises: 016_users_reputation_default
Create Date: 2026-11-21 05:10:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = '017_sync_schema_and_indexes'
down_revision = '016_users_reputation_default'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS deal_ratings (
            id SERIAL PRIMARY KEY,
            deal_id INTEGER NOT NULL REFERENCES deals(id),
            rater_id BIGINT NOT NULL REFERENCES users(telegram_id),
            target_id BIGINT NOT NULL REFERENCES users(telegram_id),
            stars INTEGER NOT NULL CHECK (stars >= 1 AND stars <= 5),
            created_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT chk_rating_different_parties CHECK (rater_id <> target_id),
            CONSTRAINT uq_rating_per_deal_rater UNIQUE (deal_id, rater_id)
        );
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_deal_ratings_deal_id ON deal_ratings (deal_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_deal_ratings_rater_id ON deal_ratings (rater_id);")
    op.execute("CREATE INDEX IF NOT EXISTS ix_deal_ratings_target_id ON deal_ratings (target_id);")

    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'chk_deals_different_parties'
            ) THEN
                ALTER TABLE deals ADD CONSTRAINT chk_deals_different_parties CHECK (seller_id <> buyer_id);
            END IF;
        END $$;
        """
    )

    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_indexes
                WHERE indexname = 'uq_open_deal_per_offer'
                  AND indexdef NOT LIKE '%DISPUTED%'
            ) THEN
                DROP INDEX uq_open_deal_per_offer;
            END IF;
            CREATE UNIQUE INDEX IF NOT EXISTS uq_open_deal_per_offer
                ON deals(offer_id)
                WHERE status NOT IN ('CANCELLED','EXPIRED','COMPLETED','DISPUTED');
        END $$;
        """
    )

    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_open_negotiation_per_offer "
        "ON negotiations(offer_id) WHERE status IN ('PENDING_MAKER','PENDING_TAKER');"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_open_negotiation_per_offer;")
    op.execute("DROP INDEX IF EXISTS uq_open_deal_per_offer;")
    op.execute("ALTER TABLE deals DROP CONSTRAINT IF EXISTS chk_deals_different_parties;")
    op.execute("DROP TABLE IF EXISTS deal_ratings;")
