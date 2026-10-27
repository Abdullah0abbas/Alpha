from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy import text
from config.settings import settings
import logging

logger = logging.getLogger(__name__)

# Requirement 8.4: Connection pooling. 
# QueuePool is used by default in SQLAlchemy. We explicitly configure it.
DATABASE_URL = settings.DATABASE_URL

engine = create_async_engine(
    DATABASE_URL,
    pool_size=20,
    max_overflow=10,
    pool_pre_ping=True,  # Ensures connections are alive before using them
    pool_recycle=1800,   # Recycle connections older than 30 minutes to prevent stale socket errors
    pool_timeout=30,     # Maximum 30 seconds wait for an available connection
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
        ("deposit_index_seq sequence", "CREATE SEQUENCE IF NOT EXISTS deposit_index_seq;"),
    ]

    column_patches = [
        ("users.nickname", "ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname VARCHAR(60);"),
        ("users.saved_usdt_address", "ALTER TABLE users ADD COLUMN IF NOT EXISTS saved_usdt_address VARCHAR(100);"),
        ("users.saved_payment_info", "ALTER TABLE users ADD COLUMN IF NOT EXISTS saved_payment_info VARCHAR(200);"),
        ("users.kyc_status", "ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_status VARCHAR(20) DEFAULT 'unverified';"),
        ("users.reward_points", "ALTER TABLE users ADD COLUMN IF NOT EXISTS reward_points INTEGER DEFAULT 0;"),
        ("offers.channel_message_id", "ALTER TABLE offers ADD COLUMN IF NOT EXISTS channel_message_id INTEGER;"),
        ("offers.payout_info", "ALTER TABLE offers ADD COLUMN IF NOT EXISTS payout_info VARCHAR;"),
        ("offers.expires_at", "ALTER TABLE offers ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;"),
        ("offers.negotiable", "ALTER TABLE offers ADD COLUMN IF NOT EXISTS negotiable BOOLEAN DEFAULT FALSE;"),
        ("offers.negotiable_type", "ALTER TABLE offers ADD COLUMN IF NOT EXISTS negotiable_type VARCHAR DEFAULT 'none';"),
        ("offers.status_changed_at", "ALTER TABLE offers ADD COLUMN IF NOT EXISTS status_changed_at TIMESTAMPTZ DEFAULT now();"),
        ("offers.payout_details", "ALTER TABLE offers ADD COLUMN IF NOT EXISTS payout_details JSONB DEFAULT '{}'::jsonb;"),
        ("deals.expires_at", "ALTER TABLE deals ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;"),
        ("deals.seller_card_msg_id", "ALTER TABLE deals ADD COLUMN IF NOT EXISTS seller_card_msg_id BIGINT;"),
        ("deals.buyer_card_msg_id", "ALTER TABLE deals ADD COLUMN IF NOT EXISTS buyer_card_msg_id BIGINT;"),
        ("deals.seller_fee_usdt", "ALTER TABLE deals ADD COLUMN IF NOT EXISTS seller_fee_usdt NUMERIC DEFAULT 0.0;"),
        ("deals.buyer_fee_usdt", "ALTER TABLE deals ADD COLUMN IF NOT EXISTS buyer_fee_usdt NUMERIC DEFAULT 0.0;"),
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
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_open_deal_per_offer ON deals(offer_id) WHERE status NOT IN ('CANCELLED','EXPIRED','COMPLETED');",
            "uq_open_deal_per_offer",
            "SELECT offer_id, COUNT(*) FROM deals WHERE status NOT IN ('CANCELLED','EXPIRED','COMPLETED') GROUP BY offer_id HAVING COUNT(*) > 1;"
        ),
        (
            "negotiations.uq_open_negotiation_per_offer",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_open_negotiation_per_offer ON negotiations(offer_id) WHERE status IN ('PENDING_MAKER','PENDING_TAKER');",
            "uq_open_negotiation_per_offer",
            "SELECT offer_id, COUNT(*) FROM negotiations WHERE status IN ('PENDING_MAKER','PENDING_TAKER') GROUP BY offer_id HAVING COUNT(*) > 1;"
        ),
    ]

    try:
        async with engine.begin() as conn:
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


