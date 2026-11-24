"""upgrade blockchain_transactions table for on-chain deposit, sweep, and payout tracking

Revision ID: 020_upgrade_blockchain_transactions
Revises: 019_add_kyc_requests
Create Date: 2026-11-24 04:05:00.000000

"""
from alembic import op


# revision identifiers, used by Alembic.
revision = "020_upgrade_blockchain_transactions"
down_revision = "019_add_kyc_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1. Add new columns with safe defaults for existing records
    op.execute(
        "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS log_index INTEGER NOT NULL DEFAULT 0;"
    )
    op.execute(
        "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS block_number BIGINT;"
    )
    op.execute(
        "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS from_address VARCHAR(64);"
    )
    op.execute(
        "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS to_address VARCHAR(64);"
    )
    op.execute(
        "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS amount_raw NUMERIC(38, 0);"
    )
    op.execute(
        "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS token_address VARCHAR(64);"
    )
    op.execute(
        "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'confirmed';"
    )
    op.execute(
        "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS nonce BIGINT;"
    )
    op.execute(
        "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();"
    )

    # 2. Dynamically locate and drop any single-column UNIQUE constraint on tx_hash from pg_constraint
    op.execute(
        """
        DO $$
        DECLARE
            r RECORD;
        BEGIN
            FOR r IN
                SELECT c.conname
                FROM pg_constraint c
                JOIN pg_attribute a
                  ON a.attrelid = c.conrelid
                 AND a.attnum = c.conkey[1]
                WHERE c.conrelid = 'blockchain_transactions'::regclass
                  AND c.contype = 'u'
                  AND array_length(c.conkey, 1) = 1
                  AND a.attname = 'tx_hash'
            LOOP
                EXECUTE format('ALTER TABLE blockchain_transactions DROP CONSTRAINT IF EXISTS %I', r.conname);
            END LOOP;
        END $$;
        """
    )

    # 3. Add composite UNIQUE constraint on (tx_hash, log_index)
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = 'blockchain_transactions'::regclass
                  AND conname = 'uq_blockchain_tx_hash_log_index'
            ) THEN
                ALTER TABLE blockchain_transactions
                    ADD CONSTRAINT uq_blockchain_tx_hash_log_index
                    UNIQUE (tx_hash, log_index);
            END IF;
        END $$;
        """
    )

    # 4. Expand chk_direction to accept deposit, sweep, release, refund, gas_topup, fee_sweep
    op.execute("ALTER TABLE blockchain_transactions DROP CONSTRAINT IF EXISTS chk_direction;")
    op.execute(
        """
        ALTER TABLE blockchain_transactions
            ADD CONSTRAINT chk_direction
            CHECK (direction IN ('deposit', 'sweep', 'release', 'refund', 'gas_topup', 'fee_sweep'));
        """
    )

    # 5. Add chk_blockchain_tx_status constraint
    op.execute("ALTER TABLE blockchain_transactions DROP CONSTRAINT IF EXISTS chk_blockchain_tx_status;")
    op.execute(
        """
        ALTER TABLE blockchain_transactions
            ADD CONSTRAINT chk_blockchain_tx_status
            CHECK (status IN ('pending', 'confirmed', 'failed', 'anomaly'));
        """
    )


def downgrade() -> None:
    # 1. Fail explicitly with a clear message if existing rows violate old constraints
    op.execute(
        """
        DO $$
        DECLARE
            bad_dir_count INTEGER;
            dup_hash_count INTEGER;
        BEGIN
            SELECT COUNT(*) INTO bad_dir_count
            FROM blockchain_transactions
            WHERE direction IS NOT NULL
              AND direction NOT IN ('deposit', 'release');

            IF bad_dir_count > 0 THEN
                RAISE EXCEPTION
                    'Cannot downgrade 020_upgrade_blockchain_transactions: found % row(s) with direction outside (deposit, release).',
                    bad_dir_count;
            END IF;

            SELECT COUNT(*) INTO dup_hash_count
            FROM (
                SELECT tx_hash
                FROM blockchain_transactions
                GROUP BY tx_hash
                HAVING COUNT(*) > 1
            ) dup;

            IF dup_hash_count > 0 THEN
                RAISE EXCEPTION
                    'Cannot downgrade 020_upgrade_blockchain_transactions: found % duplicate tx_hash value(s) across multiple log_index rows that violate single-column UNIQUE(tx_hash).',
                    dup_hash_count;
            END IF;
        END $$;
        """
    )

    # 2. Revert constraints to pre-020 state
    op.execute(
        "ALTER TABLE blockchain_transactions DROP CONSTRAINT IF EXISTS uq_blockchain_tx_hash_log_index;"
    )
    op.execute(
        "ALTER TABLE blockchain_transactions DROP CONSTRAINT IF EXISTS chk_blockchain_tx_status;"
    )
    op.execute(
        "ALTER TABLE blockchain_transactions DROP CONSTRAINT IF EXISTS chk_direction;"
    )
    op.execute(
        """
        ALTER TABLE blockchain_transactions
            ADD CONSTRAINT chk_direction
            CHECK (direction IN ('deposit', 'release'));
        """
    )
    op.execute(
        """
        ALTER TABLE blockchain_transactions
            ADD CONSTRAINT blockchain_transactions_tx_hash_key
            UNIQUE (tx_hash);
        """
    )

    # 3. Drop the columns added in 020
    op.execute("ALTER TABLE blockchain_transactions DROP COLUMN IF EXISTS created_at;")
    op.execute("ALTER TABLE blockchain_transactions DROP COLUMN IF EXISTS nonce;")
    op.execute("ALTER TABLE blockchain_transactions DROP COLUMN IF EXISTS status;")
    op.execute("ALTER TABLE blockchain_transactions DROP COLUMN IF EXISTS token_address;")
    op.execute("ALTER TABLE blockchain_transactions DROP COLUMN IF EXISTS amount_raw;")
    op.execute("ALTER TABLE blockchain_transactions DROP COLUMN IF EXISTS to_address;")
    op.execute("ALTER TABLE blockchain_transactions DROP COLUMN IF EXISTS from_address;")
    op.execute("ALTER TABLE blockchain_transactions DROP COLUMN IF EXISTS block_number;")
    op.execute("ALTER TABLE blockchain_transactions DROP COLUMN IF EXISTS log_index;")
