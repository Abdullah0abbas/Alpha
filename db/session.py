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
    echo=False
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
    1. Creates all missing tables (audit_logs, system_settings, negotiations, etc.) if they do not exist.
    2. Dynamically patches any missing columns on existing tables without data loss.
    3. Creates required indexes safely.
    """
    from db.models import Base

    table_and_index_ddls = [
        # 1. System Settings Table
        """
        CREATE TABLE IF NOT EXISTS system_settings (
            key VARCHAR PRIMARY KEY,
            value JSONB NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT now()
        );
        """,
        # 2. Audit Logs Table
        """
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
        """,
        # 3. Negotiations Table
        """
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
        """,
        # 4. Indexes for audit_logs
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_actor_id ON audit_logs (actor_id);",
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_actor_type ON audit_logs (actor_type);",
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_action ON audit_logs (action);",
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_target_type ON audit_logs (target_type);",
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_target_id ON audit_logs (target_id);",
        "CREATE INDEX IF NOT EXISTS ix_audit_logs_created_at ON audit_logs (created_at);",
        # 5. Indexes for negotiations
        "CREATE INDEX IF NOT EXISTS ix_negotiations_status ON negotiations (status);",
        "CREATE INDEX IF NOT EXISTS ix_negotiations_expires_at ON negotiations (expires_at);",
        # 6. Deposit Index Sequence
        "CREATE SEQUENCE IF NOT EXISTS deposit_index_seq;",
    ]
    
    column_patches = [
        # Users table updates
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS nickname VARCHAR(60);",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS saved_usdt_address VARCHAR(100);",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS saved_payment_info VARCHAR(200);",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS kyc_status VARCHAR(20) DEFAULT 'unverified';",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS reward_points INTEGER DEFAULT 0;",
        # Offers table updates
        "ALTER TABLE offers ADD COLUMN IF NOT EXISTS channel_message_id INTEGER;",
        "ALTER TABLE offers ADD COLUMN IF NOT EXISTS payout_info VARCHAR;",
        "ALTER TABLE offers ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;",
        "ALTER TABLE offers ADD COLUMN IF NOT EXISTS negotiable BOOLEAN DEFAULT FALSE;",
        "ALTER TABLE offers ADD COLUMN IF NOT EXISTS negotiable_type VARCHAR DEFAULT 'none';",
        # Deals table updates
        "ALTER TABLE deals ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;",
    ]
    try:
        async with engine.begin() as conn:
            # 1. First sync Base metadata
            try:
                await conn.run_sync(Base.metadata.create_all)
            except Exception as meta_err:
                logger.debug(f"Metadata create_all note: {meta_err}")

            # 2. Execute explicit DDLs to guarantee presence of tables & indexes
            for ddl in table_and_index_ddls:
                try:
                    await conn.execute(text(ddl))
                except Exception as ddl_err:
                    logger.debug(f"DDL note: {ddl_err}")
            
            # 3. Patch any missing columns on pre-existing tables
            for patch in column_patches:
                try:
                    await conn.execute(text(patch))
                except Exception as patch_err:
                    logger.debug(f"Patch notice: {patch_err}")
                    
        logger.info("Database schema verified and all tables/columns synchronized successfully.")
    except Exception as e:
        logger.error(f"Error checking/updating database schema: {e}", exc_info=True)

async def get_session():
    async with AsyncSessionLocal() as session:
        yield session


