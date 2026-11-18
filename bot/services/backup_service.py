import os
import json
import gzip
import asyncio
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from aiogram import Bot
from aiogram.types import FSInputFile
from sqlalchemy.future import select

from db.session import AsyncSessionLocal
from db.models import (
    User, Offer, Deal, Negotiation, Dispute,
    PaymentProof, BlockchainTransaction, SystemSetting, AuditLog, DealRating
)
from config.settings import settings

logger = logging.getLogger(__name__)

BACKUP_FORMAT_VERSION = "3.8"
BACKUP_DIR = os.path.join(os.getcwd(), "backups")

def serialize_val(val):
    if isinstance(val, datetime):
        return val.isoformat()
    elif isinstance(val, Decimal):
        return str(val)
    elif isinstance(val, (bytes, bytearray)):
        return val.hex()
    return val


def _write_backup_gzip_and_cleanup(filepath: str, backup_payload: dict) -> None:
    """
    Runs in a worker thread via asyncio.to_thread so JSON serialization, gzip compression,
    and disk retention cleanup never block the main asyncio event loop.
    """
    os.makedirs(BACKUP_DIR, exist_ok=True)
    json_bytes = json.dumps(backup_payload, ensure_ascii=False, indent=2).encode("utf-8")
    with gzip.open(filepath, "wb") as f:
        f.write(json_bytes)

    # Retention: keep last 30 backups on disk, delete older ones
    try:
        existing_files = sorted(
            [os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR) if f.startswith("escrow_backup_")],
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
    Exports all database tables into a clean, gzip-compressed JSON file,
    saves it to /backups/, cleans old backups, and returns:
    (file_path, filename, stats_dict)
    """
    timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    filename = f"escrow_backup_{timestamp_str}.json.gz"
    filepath = os.path.join(BACKUP_DIR, filename)

    stats = {}

    async with AsyncSessionLocal() as session:
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
                "direction": t.direction,
                "amount": serialize_val(t.amount),
                "confirmed_at": serialize_val(t.confirmed_at)
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

    backup_payload = {
        "metadata": {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "version": BACKUP_FORMAT_VERSION,
            "table_stats": stats
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
        "audit_logs": audit_data
    }

    # Offload JSON dump + gzip compression + file retention to a background thread
    await asyncio.to_thread(_write_backup_gzip_and_cleanup, filepath, backup_payload)

    return filepath, filename, stats


async def perform_and_send_backup(bot: Bot, recipient_id: int | None = None, is_scheduled: bool = False):
    """
    Creates backup file on server and dispatches it to the admin(s) via Telegram.
    """
    filepath, filename, stats = await create_database_backup()
    file_size_kb = round(os.path.getsize(filepath) / 1024, 2)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    title_tag = "⏰ **النسخة الاحتياطية اليومية المجدولة**" if is_scheduled else "💾 **نسخة احتياطية فورية لقاعدة البيانات**"

    caption = (
        f"{title_tag}\n\n"
        f"▫️ **اسم الملف:** `{filename}`\n"
        f"▫️ **حجم الملف:** `{file_size_kb}` كيلوبايت\n"
        f"▫️ **توقيت النسخ:** `{now_str}`\n"
        f"▫️ **الحالة:** محفوظة على الخادم بنجاح ✅\n\n"
        f"📊 **البيانات المضمنة:**\n"
        f"▫️ **المستخدمون:** `{stats.get('users', 0)}`\n"
        f"▫️ **الصفقات:** `{stats.get('deals', 0)}`\n"
        f"▫️ **العروض:** `{stats.get('offers', 0)}`\n"
        f"▫️ **المفاوضات:** `{stats.get('negotiations', 0)}`\n"
        f"▫️ **التقييمات:** `{stats.get('deal_ratings', 0)}`\n"
        f"▫️ **النزاعات:** `{stats.get('disputes', 0)}`\n\n"
        f"تم حفظ النسخة محلياً في مسار `/backups/`."
    )

    targets = [recipient_id] if recipient_id else settings.ADMIN_IDS

    for admin_id in targets:
        if not admin_id:
            continue
        try:
            doc = FSInputFile(path=filepath, filename=filename)
            await bot.send_document(
                chat_id=admin_id,
                document=doc,
                caption=caption,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Failed to dispatch backup document to admin {admin_id}: {e}")
