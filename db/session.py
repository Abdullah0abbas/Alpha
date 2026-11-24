from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import text
from config.settings import settings
import logging

logger = logging.getLogger(__name__)

# Requirement 8.4: Connection pooling & statement timeout protection.
# QueuePool is used by default in SQLAlchemy. We explicitly configure it.
DATABASE_URL = settings.DATABASE_URL
_connect_args = {"command_timeout": 30} if "asyncpg" in DATABASE_URL else {}

engine = create_async_engine(
    DATABASE_URL,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,  # Ensures connections are alive before using them
    pool_recycle=1800,   # Recycle connections older than 30 minutes to prevent stale socket errors
    pool_timeout=30,     # Maximum 30 seconds wait for an available connection
    connect_args=_connect_args,
    echo=False,
    hide_parameters=True
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine, 
    autocommit=False, 
    autoflush=False, 
    expire_on_commit=False
)

async def ensure_schema_updated():
    """
    Comprehensive self-healing schema guard.
    Executes DDLs in strict ordered sequence with SAVEPOINT isolation so individual failures
    never abort subsequent statements:
    (a) Tables & Sequences
    (b) Column Patches
    (c) Indexes & Constraints (with diagnostic query logging if conflicting data exists)
    """
    from db.models import Base

    table_ddls = [
        ("system_settings table", """
        CREATE TABLE IF NOT EXISTS system_settings (
            key VARCHAR PRIMARY KEY,
            value JSONB NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT now()
        );
        """),
        ("audit_logs table", """
        CREATE TABLE IF NOT EXISTS audit_logs (
            id SERIAL PRIMARY KEY,
            actor_id BIGINT,
            actor_type VARCHAR(30) DEFAULT 'ADMIN',
            action VARCHAR(50) NOT NULL,
            target_type VARCHAR(30) NOT NULL,
            target_id VARCHAR(100) NOT NULL,
            details JSONB,
            ip_or_source VARCHAR(50) DEFAULT 'TELEGRAM_BOT',
            created_at TIMESTAMPTZ DEFAULT now()
        );
        """),
        ("negotiations table", """
        CREATE TABLE IF NOT EXISTS negotiations (
            id SERIAL PRIMARY KEY,
            offer_id INTEGER NOT NULL REFERENCES offers(id),
            taker_id BIGINT NOT NULL REFERENCES users(telegram_id),
            maker_id BIGINT NOT NULL REFERENCES users(telegram_id),
            proposed_rate NUMERIC NOT NULL,
            proposed_amount NUMERIC NOT NULL,
            status VARCHAR DEFAULT 'PENDING_MAKER',
            round INTEGER DEFAULT 1,
            expires_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ DEFAULT now(),
            updated_at TIMESTAMPTZ DEFAULT now()
        );
        """),
        ("deal_ratings table", """
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
        """),
        ("kyc_requests table", """
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
        """),
        ("deposit_index_seq sequence", "CREATE SEQUENCE IF NOT EXISTS deposit_index_seq;"),
    ]

    column_patches = [
        ("users.nickname", "ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname VARCHAR(60);"),
        ("users.saved_usdt_address", "ALTER TABLE users ADD COLUMN IF NOT EXISTS saved_usdt_address VARCHAR(100);"),
        ("users.saved_payment_info", "ALTER TABLE users ADD COLUMN IF NOT EXISTS saved_payment_info VARCHAR(200);"),
        ("users.kyc_status", "ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_status VARCHAR(20) DEFAULT 'unverified';"),
        ("users.reward_points", "ALTER TABLE users ADD COLUMN IF NOT EXISTS reward_points INTEGER DEFAULT 0;"),
        ("users.reputation_score_default", "ALTER TABLE users ALTER COLUMN reputation_score SET DEFAULT 100;"),
        ("offers.channel_message_id", "ALTER TABLE offers ADD COLUMN IF NOT EXISTS channel_message_id INTEGER;"),
        ("offers.payout_info", "ALTER TABLE offers ADD COLUMN IF NOT EXISTS payout_info VARCHAR;"),
        ("offers.expires_at", "ALTER TABLE offers ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;"),
        ("offers.negotiable", "ALTER TABLE offers ADD COLUMN IF NOT EXISTS negotiable BOOLEAN DEFAULT FALSE;"),
        ("offers.negotiable_type", "ALTER TABLE offers ADD COLUMN IF NOT EXISTS negotiable_type VARCHAR DEFAULT 'none';"),
        ("offers.status_changed_at", "ALTER TABLE offers ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMPTZ DEFAULT now();"),
        ("offers.payout_details", "ALTER TABLE offers ADD COLUMN IF NOT EXISTS payout_details JSONB DEFAULT '{}'::jsonb;"),
        ("offers.note", "ALTER TABLE offers ADD COLUMN IF NOT EXISTS note VARCHAR(60);"),
        ("offers.duration_hours", "ALTER TABLE offers ADD COLUMN IF NOT EXISTS duration_hours NUMERIC DEFAULT 4.0;"),
        ("deals.expires_at", "ALTER TABLE deals ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;"),
        ("deals.seller_card_msg_id", "ALTER TABLE deals ADD COLUMN IF NOT EXISTS seller_card_msg_id BIGINT;"),
        ("deals.buyer_card_msg_id", "ALTER TABLE deals ADD COLUMN IF NOT EXISTS buyer_card_msg_id BIGINT;"),
        ("deals.seller_fee_usdt", "ALTER TABLE deals ADD COLUMN IF NOT EXISTS seller_fee_usdt NUMERIC DEFAULT 0.0;"),
        ("deals.buyer_fee_usdt", "ALTER TABLE deals ADD COLUMN IF NOT EXISTS buyer_fee_usdt NUMERIC DEFAULT 0.0;"),
        ("deals.deposit_tx_hash", "ALTER TABLE deals ADD COLUMN IF NOT EXISTS deposit_tx_hash VARCHAR(128);"),
        ("deals.payout_tx_hash", "ALTER TABLE deals ADD COLUMN IF NOT EXISTS payout_tx_hash VARCHAR(128);"),
        ("blockchain_transactions.log_index", "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS log_index INTEGER NOT NULL DEFAULT 0;"),
        ("blockchain_transactions.block_number", "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS block_number BIGINT;"),
        ("blockchain_transactions.from_address", "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS from_address VARCHAR(64);"),
        ("blockchain_transactions.to_address", "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS to_address VARCHAR(64);"),
        ("blockchain_transactions.amount_raw", "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS amount_raw NUMERIC(38, 0);"),
        ("blockchain_transactions.token_address", "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS token_address VARCHAR(64);"),
        ("blockchain_transactions.status", "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'confirmed';"),
        ("blockchain_transactions.nonce", "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS nonce BIGINT;"),
        ("blockchain_transactions.created_at", "ALTER TABLE blockchain_transactions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();"),
    ]

    index_and_constraint_ddls = [
        ("audit_logs.ix_actor_id", "CREATE INDEX IF NOT EXISTS ix_audit_logs_actor_id ON audit_logs (actor_id);", None, None),
        ("audit_logs.ix_actor_type", "CREATE INDEX IF NOT EXISTS ix_audit_logs_actor_type ON audit_logs (actor_type);", None, None),
        ("audit_logs.ix_action", "CREATE INDEX IF NOT EXISTS ix_audit_logs_action ON audit_logs (action);", None, None),
        ("audit_logs.ix_target_type", "CREATE INDEX IF NOT EXISTS ix_audit_logs_target_type ON audit_logs (target_type);", None, None),
        ("audit_logs.ix_target_id", "CREATE INDEX IF NOT EXISTS ix_audit_logs_target_id ON audit_logs (target_id);", None, None),
        ("audit_logs.ix_created_at", "CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs (created_at);", None, None),
        ("negotiations.ix_status", "CREATE INDEX IF NOT EXISTS ix_negotiations_status ON negotiations (status);", None, None),
        ("negotiations.ix_expires_at", "CREATE INDEX IF NOT EXISTS ix_negotiations_expires_at ON negotiations (expires_at);", None, None),
        ("kyc_requests.ix_user_id", "CREATE INDEX IF NOT EXISTS ix_kyc_requests_user_id ON kyc_requests (user_id);", None, None),
        ("kyc_requests.ix_status", "CREATE INDEX IF NOT EXISTS ix_kyc_requests_status ON kyc_requests (status);", None, None),
        (
            "deals.chk_deals_different_parties",
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint WHERE conname = 'chk_deals_different_parties'
                ) THEN
                    ALTER TABLE deals ADD CONSTRAINT chk_deals_different_parties CHECK (seller_id <> buyer_id);
                END IF;
            END $$;
            """,
            "chk_deals_different_parties",
            "SELECT id, seller_id, buyer_id FROM deals WHERE seller_id = buyer_id;"
        ),
        (
            "deals.uq_open_deal_per_offer",
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
            """,
            "uq_open_deal_per_offer",
            "SELECT offer_id, COUNT(*) FROM deals WHERE status NOT IN ('CANCELLED','EXPIRED','COMPLETED','DISPUTED') GROUP BY offer_id HAVING COUNT(*) > 1;"
        ),
        (
            "negotiations.uq_open_negotiation_per_offer",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_open_negotiation_per_offer ON negotiations(offer_id) WHERE status IN ('PENDING_MAKER','PENDING_TAKER');",
            "uq_open_negotiation_per_offer",
            "SELECT offer_id, COUNT(*) FROM negotiations WHERE status IN ('PENDING_MAKER','PENDING_TAKER') GROUP BY offer_id HAVING COUNT(*) > 1;"
        ),
        ("deal_ratings.ix_deal_id", "CREATE INDEX IF NOT EXISTS ix_deal_ratings_deal_id ON deal_ratings (deal_id);", None, None),
        ("deal_ratings.ix_target_id", "CREATE INDEX IF NOT EXISTS ix_deal_ratings_target_id ON deal_ratings (target_id);", None, None),
        ("deals.uq_deals_deposit_tx_hash", "CREATE UNIQUE INDEX IF NOT EXISTS uq_deals_deposit_tx_hash ON deals (deposit_tx_hash) WHERE deposit_tx_hash IS NOT NULL;", None, None),
        ("deals.uq_deals_payout_tx_hash", "CREATE UNIQUE INDEX IF NOT EXISTS uq_deals_payout_tx_hash ON deals (payout_tx_hash) WHERE payout_tx_hash IS NOT NULL;", None, None),
        (
            "blockchain_transactions.drop_single_tx_hash_unique",
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
            """,
            None,
            None,
        ),
        (
            "blockchain_transactions.uq_blockchain_tx_hash_log_index",
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
            """,
            "uq_blockchain_tx_hash_log_index",
            "SELECT tx_hash, log_index, COUNT(*) FROM blockchain_transactions GROUP BY tx_hash, log_index HAVING COUNT(*) > 1;",
        ),
        (
            "blockchain_transactions.chk_direction",
            """
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'blockchain_transactions'::regclass
                      AND conname = 'chk_direction'
                      AND pg_get_constraintdef(oid) NOT LIKE '%sweep%'
                ) THEN
                    ALTER TABLE blockchain_transactions DROP CONSTRAINT chk_direction;
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'blockchain_transactions'::regclass
                      AND conname = 'chk_direction'
                ) THEN
                    ALTER TABLE blockchain_transactions
                        ADD CONSTRAINT chk_direction
                        CHECK (direction IN ('deposit', 'sweep', 'release', 'refund', 'gas_topup', 'fee_sweep'));
                END IF;
            END $$;
            """,
            "chk_direction",
            "SELECT id, direction FROM blockchain_transactions WHERE direction IS NOT NULL AND direction NOT IN ('deposit', 'sweep', 'release', 'refund', 'gas_topup', 'fee_sweep');",
        ),
        (
            "blockchain_transactions.chk_blockchain_tx_status",
            """
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conrelid = 'blockchain_transactions'::regclass
                      AND conname = 'chk_blockchain_tx_status'
                ) THEN
                    ALTER TABLE blockchain_transactions
                        ADD CONSTRAINT chk_blockchain_tx_status
                        CHECK (status IN ('pending', 'confirmed', 'failed', 'anomaly'));
                END IF;
            END $$;
            """,
            "chk_blockchain_tx_status",
            "SELECT id, status FROM blockchain_transactions WHERE status NOT IN ('pending', 'confirmed', 'failed', 'anomaly');",
        ),
    ]

    try:
        async with engine.begin() as conn:
            # Serialize schema synchronization across processes/restarts with a transaction-level advisory lock
            await conn.execute(text("SELECT pg_advisory_xact_lock(74839201);"))

            # (a) First sync Base metadata tables
            try:
                await conn.run_sync(Base.metadata.create_all)
            except Exception as meta_err:
                logger.warning(f"Metadata create_all notice: {meta_err}")

            # (a) Tables & Sequences
            for name, sql in table_ddls:
                try:
                    async with conn.begin_nested():
                        await conn.execute(text(sql))
                except Exception as table_err:
                    logger.warning(f"Schema table warning [{name}]: {table_err}")

            # (b) Column Patches
            for name, sql in column_patches:
                try:
                    async with conn.begin_nested():
                        await conn.execute(text(sql))
                except Exception as patch_err:
                    logger.warning(f"Schema column patch warning [{name}]: {patch_err}")

            # (c) Indexes & Constraints
            for name, sql, constraint_name, diag_query in index_and_constraint_ddls:
                try:
                    async with conn.begin_nested():
                        await conn.execute(text(sql))
                except Exception as index_err:
                    if constraint_name and diag_query:
                        logger.error(
                            f"Failed to apply constraint/index '{constraint_name}' due to existing conflicting data: {index_err}. "
                            f"Diagnostic query: {diag_query}"
                        )
                    else:
                        logger.warning(f"Schema index warning [{name}]: {index_err}")

        logger.info("Database schema verified and all tables/columns/indexes synchronized successfully.")
    except Exception as e:
        logger.error(f"Error checking/updating database schema: {e}", exc_info=True)

async def get_session():
    async with AsyncSessionLocal() as session:
        yield session


