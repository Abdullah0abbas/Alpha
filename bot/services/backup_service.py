import os
import json
import gzip
import time
import asyncio
import logging
from datetime import datetime, timezone
from decimal import Decimal
from cryptography.fernet import Fernet
from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy import text
from sqlalchemy.future import select

from db.session import AsyncSessionLocal
from db.models import (
    User, Offer, Deal, Negotiation, Dispute,
    PaymentProof, BlockchainTransaction, SystemSetting, AuditLog, DealRating, KycRequest
)
from config.settings import settings

logger = logging.getLogger(__name__)

BACKUP_FORMAT_VERSION = "4.1"
BACKUP_DIR = os.path.join(os.getcwd(), "backups")

_LAST_MISSING_KEY_ALERT_TS: float | None = None
_MISSING_KEY_ALERT_COOLDOWN_SECONDS = 86400  # 24 hours (once per period)


class BackupEncryptionError(RuntimeError):
    """Raised when BACKUP_ENCRYPTION_KEY is missing or invalid."""


def _get_fernet_cipher(raw_key: str | None = None) -> Fernet:
    key_str = (
        raw_key
        if raw_key is not None
        else (os.getenv("BACKUP_ENCRYPTION_KEY") or settings.BACKUP_ENCRYPTION_KEY or "")
    ).strip()
    if not key_str:
        raise BackupEncryptionError(
            "متغير البيئة BACKUP_ENCRYPTION_KEY غير مضبوط. "
            "يُمنع حفظ أو إرسال أي نسخة احتياطية غير مشفرة."
        )
    try:
        return Fernet(key_str.encode("utf-8"))
    except Exception as exc:
        raise BackupEncryptionError(
            "قيمة BACKUP_ENCRYPTION_KEY غير صالحة (يجب أن تكون مفتاح Fernet بصيغة Base64 بطول 32 بايت). "
            "يُمنع حفظ أو إرسال أي نسخة احتياطية غير مشفرة."
        ) from exc


def encrypt_backup_payload(backup_payload: dict, raw_key: str | None = None) -> bytes:
    """
    Serializes, gzip-compresses, and Fernet-encrypts the backup dictionary strictly in memory.
    Never writes plaintext or unencrypted gzip bytes to disk.
    """
    fernet = _get_fernet_cipher(raw_key)
    json_bytes = json.dumps(backup_payload, ensure_ascii=False, indent=2).encode("utf-8")
    compressed_bytes = gzip.compress(json_bytes)
    return fernet.encrypt(compressed_bytes)


def serialize_val(val):
    if isinstance(val, datetime):
        return val.isoformat()
    elif isinstance(val, Decimal):
        return str(val)
    elif isinstance(val, (bytes, bytearray)):
        return val.hex()
    return val


def _write_encrypted_backup_and_cleanup(filepath: str, backup_payload: dict) -> None:
    """
    Runs in a worker thread via asyncio.to_thread so JSON serialization, in-memory gzip compression,
    in-memory Fernet encryption, disk write of ciphertext (.json.gz.enc), and retention cleanup
    never block the main asyncio event loop.
    """
    encrypted_bytes = encrypt_backup_payload(backup_payload)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    with open(filepath, "wb") as f:
        f.write(encrypted_bytes)
    try:
        os.chmod(filepath, 0o600)
    except OSError:
        pass

    # Retention: keep last 30 encrypted backups (.json.gz.enc) on disk; never touch legacy .json.gz files
    try:
        existing_files = sorted(
            [
                os.path.join(BACKUP_DIR, f)
                for f in os.listdir(BACKUP_DIR)
                if f.startswith("escrow_backup_") and f.endswith(".json.gz.enc")
            ],
            key=os.path.getmtime
        )
        if len(existing_files) > 30:
            for old_file in existing_files[:-30]:
                try:
                    os.remove(old_file)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"Error during backup retention cleanup: {e}")


async def create_database_backup() -> tuple[str, str, dict]:
    """
    Exports all database tables into an in-memory gzip-compressed, Fernet-encrypted file (.json.gz.enc),
    saves ciphertext to /backups/, cleans old .json.gz.enc backups (keeping last 30), and returns:
    (file_path, filename, stats_dict)
    """
    # Fail fast before opening DB session or touching disk if key is missing/invalid
    _get_fernet_cipher()

    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"escrow_backup_{timestamp_str}.json.gz.enc"
    filepath = os.path.join(BACKUP_DIR, filename)

    stats = {}

    async with AsyncSessionLocal() as session:
        # Enforce a single consistent point-in-time snapshot across all tables & deposit_index_seq
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))

        # 1. Users
        users_res = await session.execute(select(User))
        users = users_res.scalars().all()
        stats["users"] = len(users)
        users_data = [
            {
                "telegram_id": u.telegram_id,
                "username": u.username,
                "nickname": u.nickname,
                "saved_usdt_address": u.saved_usdt_address,
                "saved_payment_info": u.saved_payment_info,
                "kyc_status": u.kyc_status,
                "reward_points": u.reward_points,
                "reputation_score": serialize_val(u.reputation_score),
                "completed_deals_count": u.completed_deals_count,
                "is_banned": u.is_banned,
                "created_at": serialize_val(u.created_at)
            }
            for u in users
        ]

        # 2. Offers
        offers_res = await session.execute(select(Offer))
        offers = offers_res.scalars().all()
        stats["offers"] = len(offers)
        offers_data = [
            {
                "id": o.id,
                "user_id": o.user_id,
                "offer_type": o.offer_type,
                "rate": serialize_val(o.rate),
                "min_limit": serialize_val(o.min_limit),
                "max_limit": serialize_val(o.max_limit),
                "payment_methods": o.payment_methods,
                "status": o.status,
                "status_changed_at": serialize_val(o.status_changed_at),
                "channel_message_id": o.channel_message_id,
                "payout_info": o.payout_info,
                "payout_details": o.payout_details,
                "note": getattr(o, "note", None),
                "duration_hours": serialize_val(getattr(o, "duration_hours", None)),
                "expires_at": serialize_val(o.expires_at),
                "negotiable": o.negotiable,
                "negotiable_type": o.negotiable_type,
                "created_at": serialize_val(o.created_at)
            }
            for o in offers
        ]

        # 3. Deals
        deals_res = await session.execute(select(Deal))
        deals = deals_res.scalars().all()
        stats["deals"] = len(deals)
        deals_data = [
            {
                "id": d.id,
                "offer_id": d.offer_id,
                "seller_id": d.seller_id,
                "buyer_id": d.buyer_id,
                "amount_usdt": serialize_val(d.amount_usdt),
                "amount_syp": serialize_val(d.amount_syp),
                "fee_usdt": serialize_val(d.fee_usdt),
                "seller_fee_usdt": serialize_val(getattr(d, "seller_fee_usdt", None)),
                "buyer_fee_usdt": serialize_val(getattr(d, "buyer_fee_usdt", None)),
                "deposit_address": d.deposit_address,
                "deposit_index": d.deposit_index,
                "buyer_payout_address": d.buyer_payout_address,
                "seller_payment_info": d.seller_payment_info,
                "status": d.status,
                "expires_at": serialize_val(d.expires_at),
                "seller_card_msg_id": d.seller_card_msg_id,
                "buyer_card_msg_id": d.buyer_card_msg_id,
                "deposit_tx_hash": getattr(d, "deposit_tx_hash", None),
                "payout_tx_hash": getattr(d, "payout_tx_hash", None),
                "created_at": serialize_val(d.created_at),
                "updated_at": serialize_val(d.updated_at)
            }
            for d in deals
        ]

        # 4. Negotiations
        negs_res = await session.execute(select(Negotiation))
        negs = negs_res.scalars().all()
        stats["negotiations"] = len(negs)
        negs_data = [
            {
                "id": n.id,
                "offer_id": n.offer_id,
                "taker_id": n.taker_id,
                "maker_id": n.maker_id,
                "proposed_rate": serialize_val(n.proposed_rate),
                "proposed_amount": serialize_val(n.proposed_amount),
                "status": n.status,
                "round": n.round,
                "expires_at": serialize_val(n.expires_at),
                "created_at": serialize_val(n.created_at),
                "updated_at": serialize_val(n.updated_at)
            }
            for n in negs
        ]

        # 5. Disputes
        disp_res = await session.execute(select(Dispute))
        disps = disp_res.scalars().all()
        stats["disputes"] = len(disps)
        disps_data = [
            {
                "id": dp.id,
                "deal_id": dp.deal_id,
                "opened_by": dp.opened_by,
                "reason": dp.reason,
                "resolution": dp.resolution,
                "resolved_by": dp.resolved_by,
                "resolved_at": serialize_val(dp.resolved_at)
            }
            for dp in disps
        ]

        # 6. Deal Ratings
        ratings_res = await session.execute(select(DealRating))
        ratings = ratings_res.scalars().all()
        stats["deal_ratings"] = len(ratings)
        ratings_data = [
            {
                "id": r.id,
                "deal_id": r.deal_id,
                "rater_id": r.rater_id,
                "target_id": r.target_id,
                "stars": r.stars,
                "created_at": serialize_val(r.created_at)
            }
            for r in ratings
        ]

        # 7. Payment Proofs
        proofs_res = await session.execute(select(PaymentProof))
        proofs = proofs_res.scalars().all()
        stats["payment_proofs"] = len(proofs)
        proofs_data = [
            {
                "id": p.id,
                "deal_id": p.deal_id,
                "transaction_reference": p.transaction_reference,
                "amount": serialize_val(p.amount),
                "submitted_by": p.submitted_by,
                "submitted_at": serialize_val(p.submitted_at)
            }
            for p in proofs
        ]

        # 8. Blockchain Transactions
        tx_res = await session.execute(select(BlockchainTransaction))
        txs = tx_res.scalars().all()
        stats["blockchain_transactions"] = len(txs)
        txs_data = [
            {
                "id": t.id,
                "deal_id": t.deal_id,
                "tx_hash": t.tx_hash,
                "log_index": getattr(t, "log_index", 0),
                "direction": t.direction,
                "amount": serialize_val(t.amount),
                "amount_raw": serialize_val(getattr(t, "amount_raw", None)),
                "token_address": getattr(t, "token_address", None),
                "from_address": getattr(t, "from_address", None),
                "to_address": getattr(t, "to_address", None),
                "block_number": getattr(t, "block_number", None),
                "status": getattr(t, "status", "confirmed"),
                "nonce": getattr(t, "nonce", None),
                "confirmed_at": serialize_val(t.confirmed_at),
                "created_at": serialize_val(getattr(t, "created_at", None)),
            }
            for t in txs
        ]

        # 9. System Settings
        settings_res = await session.execute(select(SystemSetting))
        settings_rows = settings_res.scalars().all()
        stats["system_settings"] = len(settings_rows)
        settings_data = [
            {
                "key": s.key,
                "value": s.value,
                "updated_at": serialize_val(s.updated_at)
            }
            for s in settings_rows
        ]

        # 10. Audit Logs
        audit_res = await session.execute(select(AuditLog))
        audit_rows = audit_res.scalars().all()
        stats["audit_logs"] = len(audit_rows)
        audit_data = [
            {
                "id": a.id,
                "actor_id": a.actor_id,
                "actor_type": a.actor_type,
                "action": a.action,
                "target_type": a.target_type,
                "target_id": a.target_id,
                "details": a.details,
                "ip_or_source": a.ip_or_source,
                "created_at": serialize_val(a.created_at)
            }
            for a in audit_rows
        ]

        # 11. KYC Requests
        kyc_res = await session.execute(select(KycRequest))
        kyc_rows = kyc_res.scalars().all()
        stats["kyc_requests"] = len(kyc_rows)
        kyc_data = [
            {
                "id": k.id,
                "user_id": k.user_id,
                "full_name": k.full_name,
                "national_id": k.national_id,
                "phone_number": k.phone_number,
                "id_front_file_id": k.id_front_file_id,
                "id_back_file_id": k.id_back_file_id,
                "selfie_file_id": k.selfie_file_id,
                "status": k.status,
                "rejection_reason": k.rejection_reason,
                "reviewed_by": k.reviewed_by,
                "reviewed_at": serialize_val(k.reviewed_at),
                "created_at": serialize_val(k.created_at)
            }
            for k in kyc_rows
        ]

        # 12. deposit_index_seq state
        deposit_seq_info = {"last_value": 1, "is_called": False}
        try:
            seq_res = await session.execute(
                text("SELECT last_value, is_called FROM deposit_index_seq")
            )
            seq_row = seq_res.first()
            if seq_row is not None:
                deposit_seq_info = {
                    "last_value": int(seq_row[0]),
                    "is_called": bool(seq_row[1]),
                }
        except Exception as seq_err:
            logger.warning(f"Failed to read deposit_index_seq during backup: {seq_err}")

    backup_payload = {
        "metadata": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "version": BACKUP_FORMAT_VERSION,
            "table_stats": stats,
            "deposit_index_seq": deposit_seq_info,
        },
        "users": users_data,
        "offers": offers_data,
        "deals": deals_data,
        "negotiations": negs_data,
        "disputes": disps_data,
        "deal_ratings": ratings_data,
        "payment_proofs": proofs_data,
        "blockchain_transactions": txs_data,
        "system_settings": settings_data,
        "audit_logs": audit_data,
        "kyc_requests": kyc_data
    }

    # Offload JSON dump + in-memory gzip + in-memory Fernet encryption + file retention to a background thread
    await asyncio.to_thread(_write_encrypted_backup_and_cleanup, filepath, backup_payload)

    return filepath, filename, stats


async def perform_and_send_backup(bot: Bot, recipient_id: int | None = None, is_scheduled: bool = False):
    """
    Creates encrypted backup file (.json.gz.enc) on server and dispatches it to the admin(s) via Telegram.
    Never writes or sends an unencrypted backup.
    """
    global _LAST_MISSING_KEY_ALERT_TS
    from bot.services.admin_notifier import dispatch_admin_notification

    try:
        filepath, filename, stats = await create_database_backup()
    except BackupEncryptionError as key_err:
        logger.error(f"Backup aborted due to missing/invalid BACKUP_ENCRYPTION_KEY: {key_err}")
        if is_scheduled:
            now_mono = time.monotonic()
            if (
                _LAST_MISSING_KEY_ALERT_TS is None
                or (now_mono - _LAST_MISSING_KEY_ALERT_TS) >= _MISSING_KEY_ALERT_COOLDOWN_SECONDS
            ):
                _LAST_MISSING_KEY_ALERT_TS = now_mono
                alert_text = (
                    "⚠️ **تنبيه أمني — تعذّر إنشاء النسخة الاحتياطية المجدولة**\n\n"
                    f"▫️ **السبب:** {key_err}\n"
                    "▫️ **الإجراء المتخذ:** تم إيقاف حفظ وإرسال النسخة الاحتياطية لحماية البيانات ومنع إرسال أي ملف غير مشفر.\n\n"
                    "💡 يرجى ضبط متغير البيئة `BACKUP_ENCRYPTION_KEY` في ملف `.env`."
                )
                try:
                    await dispatch_admin_notification(
                        bot=bot,
                        category="backups",
                        text=alert_text,
                        parse_mode="Markdown",
                    )
                except Exception as notify_err:
                    logger.error(f"Failed to send missing backup key alert: {notify_err}")
            return
        raise

    file_size_kb = round(os.path.getsize(filepath) / 1024, 2)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    title_tag = "⏰ **النسخة الاحتياطية اليومية المجدولة**" if is_scheduled else "💾 **نسخة احتياطية فورية لقاعدة البيانات**"

    caption = (
        f"{title_tag}\n\n"
        f"▫️ **اسم الملف:** `{filename}`\n"
        f"▫️ **حجم الملف:** `{file_size_kb}` كيلوبايت\n"
        f"▫️ **توقيت النسخ:** `{now_str}`\n"
        f"▫️ **الحالة:** محفوظة ومشفرة (Fernet) بنجاح 🔒✅\n\n"
        f"📊 **البيانات المضمنة:**\n"
        f"▫️ **المستخدمون:** `{stats.get('users', 0)}`\n"
        f"▫️ **الصفقات:** `{stats.get('deals', 0)}`\n"
        f"▫️ **العروض:** `{stats.get('offers', 0)}`\n"
        f"▫️ **المفاوضات:** `{stats.get('negotiations', 0)}`\n"
        f"▫️ **التقييمات:** `{stats.get('deal_ratings', 0)}`\n"
        f"▫️ **النزاعات:** `{stats.get('disputes', 0)}`\n\n"
        f"تم حفظ النسخة المشفرة محلياً في مسار `/backups/`."
    )

    if recipient_id:
        # Manual backup triggered by an admin: send to that admin AND archive in ADMIN_BACKUPS_CHANNEL_ID if configured
        try:
            doc = FSInputFile(path=filepath, filename=filename)
            await bot.send_document(
                chat_id=recipient_id,
                document=doc,
                caption=caption,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to dispatch backup document to admin {recipient_id}: {e}")

        if settings.ADMIN_BACKUPS_CHANNEL_ID and str(recipient_id) != str(settings.ADMIN_BACKUPS_CHANNEL_ID):
            await dispatch_admin_notification(
                bot=bot,
                category="backups",
                text=caption,
                parse_mode="Markdown",
                document_path=filepath,
                document_filename=filename,
                exclude_admin_id=recipient_id
            )
    else:
        # Scheduled daily backup: dispatch to ADMIN_BACKUPS_CHANNEL_ID (or fallback to ADMIN_IDS)
        await dispatch_admin_notification(
            bot=bot,
            category="backups",
            text=caption,
            parse_mode="Markdown",
            document_path=filepath,
            document_filename=filename
        )

