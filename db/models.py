from datetime import datetime
from decimal import Decimal
from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

class Base(DeclarativeBase):
    pass

class User(Base):
    __tablename__ = "users"

    # Indexed implicitly because it's the Primary Key (Requirement 8.3)
    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String)
    nickname: Mapped[str | None] = mapped_column(String(60), nullable=True)
    saved_usdt_address: Mapped[str | None] = mapped_column(String(100), nullable=True)
    saved_payment_info: Mapped[str | None] = mapped_column(String(200), nullable=True)
    kyc_status: Mapped[str] = mapped_column(String(20), server_default="unverified", default="unverified") # unverified, pending, verified
    reward_points: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    reputation_score: Mapped[Decimal] = mapped_column(Numeric, server_default="100", default=Decimal("100"))
    completed_deals_count: Mapped[int] = mapped_column(Integer, server_default="0")
    is_banned: Mapped[bool] = mapped_column(Boolean, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class Offer(Base):
    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.telegram_id"))
    offer_type: Mapped[str | None] = mapped_column(String)
    rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    min_limit: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    max_limit: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    payment_methods: Mapped[list[str] | None] = mapped_column(ARRAY(String))
    # Status lifecycle: 'active', 'pending_approval', 'reserved', 'negotiating', 'completed', 'expired', 'cancelled', 'deleted'
    status: Mapped[str | None] = mapped_column(String, server_default="active")
    channel_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payout_info: Mapped[str | None] = mapped_column(String, nullable=True)
    payout_details: Mapped[dict | None] = mapped_column(JSONB, server_default="{}", default=dict, nullable=True)
    note: Mapped[str | None] = mapped_column(String(60), nullable=True)
    duration_hours: Mapped[Decimal | None] = mapped_column(Numeric, nullable=True, server_default="4.0")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    negotiable: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)
    negotiable_type: Mapped[str] = mapped_column(String, server_default="none", default="none")
    status_changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("offer_type IN ('sell', 'buy')", name="chk_offer_type"),
    )

class Deal(Base):
    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offer_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("offers.id"))
    seller_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.telegram_id"))
    buyer_id: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.telegram_id"))
    amount_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    amount_syp: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    fee_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False, server_default="0.0")
    seller_fee_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False, server_default="0.0")
    buyer_fee_usdt: Mapped[Decimal] = mapped_column(Numeric, nullable=False, server_default="0.0")
    
    # Requirement 5.1 (UNIQUE) & Requirement 8.3 (INDEXED)
    deposit_address: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    
    # Requirement 5.1 (UNIQUE)
    deposit_index: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    
    buyer_payout_address: Mapped[str] = mapped_column(String, nullable=False)
    seller_payment_info: Mapped[dict] = mapped_column(JSONB, nullable=False)
    
    # Requirement 8.3 (INDEXED) - Updated default to PENDING_OWNER_APPROVAL in v2.6
    status: Mapped[str] = mapped_column(String, nullable=False, server_default="PENDING_OWNER_APPROVAL", index=True)
    
    # Expiry timestamp (15 minutes after entering AWAITING_USDT_DEPOSIT in v2.6)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)

    seller_card_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    buyer_card_msg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    deposit_tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payout_tx_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        CheckConstraint("seller_id <> buyer_id", name="chk_deals_different_parties"),
        Index(
            "uq_open_deal_per_offer",
            "offer_id",
            unique=True,
            postgresql_where=text("status NOT IN ('CANCELLED','EXPIRED','COMPLETED','DISPUTED')")
        ),
        Index(
            "uq_deals_deposit_tx_hash",
            "deposit_tx_hash",
            unique=True,
            postgresql_where=text("deposit_tx_hash IS NOT NULL")
        ),
        Index(
            "uq_deals_payout_tx_hash",
            "payout_tx_hash",
            unique=True,
            postgresql_where=text("payout_tx_hash IS NOT NULL")
        ),
    )

class PaymentProof(Base):
    __tablename__ = "payment_proofs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deal_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("deals.id"))
    
    # Requirement 5.1 (UNIQUE) & Requirement 8.3 (INDEXED)
    transaction_reference: Mapped[str] = mapped_column(String, nullable=False, unique=True, index=True)
    
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    submitted_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.telegram_id"))
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

class Dispute(Base):
    __tablename__ = "disputes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deal_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("deals.id"))
    opened_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.telegram_id"))
    reason: Mapped[str | None] = mapped_column(String)
    resolution: Mapped[str | None] = mapped_column(String)
    resolved_by: Mapped[int | None] = mapped_column(BigInteger, ForeignKey("users.telegram_id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class DealRating(Base):
    __tablename__ = "deal_ratings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deal_id: Mapped[int] = mapped_column(Integer, ForeignKey("deals.id"), nullable=False, index=True)
    rater_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)
    target_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)
    stars: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        CheckConstraint("stars >= 1 AND stars <= 5", name="chk_rating_stars"),
        CheckConstraint("rater_id <> target_id", name="chk_rating_different_parties"),
        UniqueConstraint("deal_id", "rater_id", name="uq_rating_per_deal_rater"),
    )

class BlockchainTransaction(Base):
    __tablename__ = "blockchain_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deal_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("deals.id"))
    tx_hash: Mapped[str] = mapped_column(String, nullable=False)
    log_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    direction: Mapped[str | None] = mapped_column(String)
    amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    amount_raw: Mapped[Decimal | None] = mapped_column(Numeric(38, 0), nullable=True)
    token_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    from_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    to_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    block_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="confirmed", server_default="confirmed")
    nonce: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "direction IN ('deposit', 'sweep', 'release', 'refund', 'gas_topup', 'fee_sweep')",
            name="chk_direction",
        ),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'failed', 'anomaly')",
            name="chk_blockchain_tx_status",
        ),
        UniqueConstraint("tx_hash", "log_index", name="uq_blockchain_tx_hash_log_index"),
    )

class Negotiation(Base):
    __tablename__ = "negotiations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    offer_id: Mapped[int] = mapped_column(Integer, ForeignKey("offers.id"), nullable=False)
    taker_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    maker_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), nullable=False)
    proposed_rate: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    proposed_amount: Mapped[Decimal] = mapped_column(Numeric, nullable=False)
    # Status: 'PENDING_MAKER', 'PENDING_TAKER', 'ACCEPTED', 'REJECTED', 'EXPIRED'
    status: Mapped[str] = mapped_column(String, server_default="PENDING_MAKER", index=True)
    round: Mapped[int] = mapped_column(Integer, server_default="1", default=1)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        Index(
            "uq_open_negotiation_per_offer",
            "offer_id",
            unique=True,
            postgresql_where=text("status IN ('PENDING_MAKER','PENDING_TAKER')")
        ),
    )

class SystemSetting(Base):
    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[dict | bool | str | int | float] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

class AuditLog(Base):
    """
    Immutable Financial & Administrative Audit Trail table.
    Tracks all critical operations (deal releases, cancellations, bans, maintenance, dispute resolutions).
    """
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True) # Admin or user telegram_id, or None for SYSTEM
    actor_type: Mapped[str] = mapped_column(String(30), default="ADMIN", index=True) # ADMIN, USER, SYSTEM
    action: Mapped[str] = mapped_column(String(50), nullable=False, index=True) # e.g. RELEASE_ESCROW, BAN_USER, UNBAN_USER, MAINTENANCE_ON, etc.
    target_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True) # deal, user, setting, dispute
    target_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True) # ID of target object
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True) # Contextual metadata, before/after states
    ip_or_source: Mapped[str | None] = mapped_column(String(50), nullable=True, default="TELEGRAM_BOT")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class KycRequest(Base):
    """
    Identity Verification (KYC) submission record.
    Stores full name, national ID, phone number, and Telegram file_ids for front ID, back ID, and selfie with paper.
    """
    __tablename__ = "kyc_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.telegram_id"), nullable=False, index=True)
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    national_id: Mapped[str] = mapped_column(String(40), nullable=False)
    phone_number: Mapped[str] = mapped_column(String(40), nullable=False)
    id_front_file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    id_back_file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    selfie_file_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(20), server_default="pending", default="pending", index=True)  # pending, approved, rejected
    rejection_reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())



