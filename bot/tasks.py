import asyncio
from datetime import datetime, timezone, timedelta
import logging
from sqlalchemy.future import select

from db.session import AsyncSessionLocal
from db.models import Deal, Offer, Negotiation
from bot.services.channel import update_channel_offer_message
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from bot.utils.formatters import format_usdt, format_syp
from config.settings import settings

from bot.services.backup_service import perform_and_send_backup

logger = logging.getLogger(__name__)

async def start_daily_backup_scheduler(bot: Bot, target_hour_utc: int = 0, target_minute_utc: int = 0):
    """
    Daily background scheduler running at 00:00 UTC (03:00 AM Damascus / Riyadh time):
    1. Dumps and gzips full database into /backups/ on the server.
    2. Keeps 30-day rolling retention.
    3. Sends the backup document directly to all Admin Telegram accounts.
    """
    logger.info(f"Daily automated backup scheduler initialized (runs at {target_hour_utc:02d}:{target_minute_utc:02d} UTC).")
    while True:
        try:
            now = datetime.now(timezone.utc)
            # Calculate next run timestamp
            next_run = now.replace(hour=target_hour_utc, minute=target_minute_utc, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            
            sleep_duration = (next_run - now).total_seconds()
            logger.info(f"Next automated database backup scheduled in {int(sleep_duration)} seconds at {next_run.isoformat()}.")
            await asyncio.sleep(sleep_duration)

            logger.info("Executing daily automated database backup...")
            await perform_and_send_backup(bot, is_scheduled=True)
            logger.info("Daily automated database backup executed and dispatched successfully.")

        except asyncio.CancelledError:
            logger.info("Daily backup scheduler cancelled.")
            break
        except Exception as e:
            logger.error(f"Error in daily backup scheduler: {e}", exc_info=True)
            # Wait 5 minutes before retrying on failure
            await asyncio.sleep(300)

from bot.utils.deal_card import push_deal_card
from bot.utils.transitions import (
    can_expire_deal,
    can_expire_active_offer,
    can_remind_syp_payment,
    can_expire_negotiation,
    resolve_deal_parties,
    should_release_orphan_offer,
)

async def process_expiries(bot: Bot) -> tuple[int, int, int, int, int]:
    """
    Unified watcher handling timeouts and reminders item-by-item:
    1. Deals in PENDING_OWNER_APPROVAL, AWAITING_USDT_DEPOSIT, CREATED where expires_at <= now.
    2. Offers in active status where expires_at <= now (immediate permanent deletion).
    3. Deals in AWAITING_SYP_PAYMENT where expires_at <= now (one-time 30-min buyer reminder, deal stays open).
    4. Negotiations in PENDING_MAKER, PENDING_TAKER where expires_at <= now.
    5. Orphan reserved/negotiating offers older than 10 minutes.

    Uses atomic item-by-item transactions with FOR UPDATE SKIP LOCKED and pure transitions.
    Max 100 items processed per cycle per type.
    """
    now = datetime.now(timezone.utc)
    expired_deals_count = 0
    deleted_offers_count = 0
    syp_reminders_count = 0
    expired_negs_count = 0

    # ----------------------------------------------------
    # Branch 1: Expired Deals (Max 100 per cycle)
    # ----------------------------------------------------
    deal_ids = []
    try:
        async with AsyncSessionLocal() as read_session:
            deal_stmt = select(Deal.id).where(
                Deal.status.in_(["PENDING_OWNER_APPROVAL", "AWAITING_USDT_DEPOSIT", "CREATED"]),
                Deal.expires_at.is_not(None),
                Deal.expires_at <= now
            ).limit(100)
            deal_res = await read_session.execute(deal_stmt)
            deal_ids = list(deal_res.scalars().all())
    except Exception as e:
        logger.error(f"Error querying candidate expired deal IDs: {e}", exc_info=True)

    for deal_id in deal_ids:
        try:
            async with AsyncSessionLocal() as item_session:
                deal_res = await item_session.execute(
                    select(Deal).where(Deal.id == deal_id).with_for_update(skip_locked=True)
                )
                deal = deal_res.scalar_one_or_none()
                if not deal:
                    continue

                if not can_expire_deal(deal.status, deal.expires_at, now):
                    continue

                deal_id_val = deal.id
                offer_id_val = deal.offer_id
                buyer_id_val = deal.buyer_id
                seller_id_val = deal.seller_id
                amount_usdt_val = deal.amount_usdt
                amount_syp_val = deal.amount_syp

                prev_status = deal.status
                deal.status = "CANCELLED"

                target_offer = None
                maker_id_val = None

                if offer_id_val:
                    offer_res = await item_session.execute(
                        select(Offer).where(Offer.id == offer_id_val).with_for_update()
                    )
                    offer = offer_res.scalar_one_or_none()
                    if offer:
                        target_offer = offer
                        maker_id_val = offer.user_id

                        if prev_status == "PENDING_OWNER_APPROVAL":
                            # Rule 4: Offer creator unresponsive for 30 mins -> Delete offer permanently from channel & DB
                            offer.status = "deleted"
                            offer.status_changed_at = now
                        elif prev_status in ("AWAITING_USDT_DEPOSIT", "CREATED"):
                            # Rule 2: USDT Deposit 15-min timeout -> Cancel deal & Revert offer to active in channel
                            if offer.status == "reserved":
                                offer.status = "active"
                                offer.status_changed_at = now

                resolved_maker_id, resolved_taker_id = resolve_deal_parties(maker_id_val, seller_id_val, buyer_id_val)

                await item_session.commit()
                expired_deals_count += 1
                logger.info(f"Deal #{deal_id_val} ({prev_status}) cancelled due to timeout at {now.isoformat()}")

                # Post-commit notifications
                try:
                    await push_deal_card(deal_id_val, bot, notify_handoff=False)
                except Exception as e:
                    logger.warning(f"Failed to push deal card on cancellation for deal #{deal_id_val}: {e}")

                if target_offer:
                    try:
                        await update_channel_offer_message(bot, target_offer)
                    except Exception as e:
                        logger.warning(f"Failed to update channel message for offer #{offer_id_val}: {e}")

                if prev_status == "PENDING_OWNER_APPROVAL":
                    # Rule 4: Taker & Maker notifications for 30-min approval timeout
                    if resolved_taker_id:
                        try:
                            await bot.send_message(
                                resolved_taker_id,
                                f"⌛ **إلغاء الطلب — صفقة #{deal_id_val}**\n\n"
                                f"تم إلغاء طلب الصفقة وحذف العرض من القناة والسوق لعدم استجابة صاحب العرض خلال مهلة 30 دقيقة.\n"
                                f"يمكنك تصفح العروض الأخرى المتاحة عبر /offers.",
                                parse_mode="Markdown"
                            )
                        except Exception as e:
                            logger.warning(f"Failed to notify taker {resolved_taker_id} of approval timeout: {e}")

                    if resolved_maker_id:
                        try:
                            await bot.send_message(
                                resolved_maker_id,
                                f"🗑️ **تم حذف عرضك رقم #{offer_id_val} وإلغاء الطلب**\n\n"
                                f"انقضت مهلة 30 دقيقة دون اتخاذ قرار (موافقة أو رفض) على طلب الصفقة #{deal_id_val}.\n"
                                f"تم إلغاء الطلب وحذف عرضك من القناة والسوق لعدم الاستجابة.",
                                parse_mode="Markdown"
                            )
                        except Exception as e:
                            logger.warning(f"Failed to notify maker {resolved_maker_id} of approval timeout: {e}")
                else:
                    # Rule 2: 15-min USDT Deposit timeout notifications
                    deposit_text = (
                        f"⌛ **انتهت مهلة الإيداع — صفقة #{deal_id_val}**\n\n"
                        f"تم إلغاء الصفقة تلقائياً لعدم إيداع رصيد الضمان في المحفظة خلال مهلة 15 دقيقة.\n"
                        f"تمت إعادة العرض إلى قائمة العروض النشطة في السوق."
                    )
                    if buyer_id_val:
                        try:
                            await bot.send_message(buyer_id_val, deposit_text, parse_mode="Markdown")
                        except Exception as e:
                            logger.warning(f"Failed to notify buyer {buyer_id_val} of deposit timeout: {e}")
                    if seller_id_val and seller_id_val != buyer_id_val:
                        try:
                            await bot.send_message(seller_id_val, deposit_text, parse_mode="Markdown")
                        except Exception as e:
                            logger.warning(f"Failed to notify seller {seller_id_val} of deposit timeout: {e}")

                    # Admin alert detailing both counterparties & deal ID
                    admin_alert_text = (
                        f"🚨 **إلغاء صفقة — انقضاء مهلة الإيداع**\n\n"
                        f"▫️ **رقم الصفقة:** #{deal_id_val}\n"
                        f"▫️ **البائع:** `{seller_id_val}`\n"
                        f"▫️ **المشتري:** `{buyer_id_val}`\n"
                        f"▫️ **الكمية:** `{format_usdt(amount_usdt_val)} USDT`\n"
                        f"▫️ **المقابل:** `{format_syp(amount_syp_val)}` ل.س\n\n"
                        f"تم إلغاء الصفقة لعدم إيداع الضمان خلال 15 دقيقة، وإعادة العرض متاحاً في القناة."
                    )
                    for admin_id in settings.ADMIN_IDS:
                        try:
                            await bot.send_message(admin_id, admin_alert_text, parse_mode="Markdown")
                        except Exception as e:
                            logger.warning(f"Failed to notify admin {admin_id} of deposit timeout on deal #{deal_id_val}: {e}")
        except Exception as item_err:
            logger.error(f"Error expiring deal #{deal_id}: {item_err}", exc_info=True)

    # ----------------------------------------------------
    # Branch 2: Active Offers Duration Expiry -> Immediate Permanent Deletion
    # ----------------------------------------------------
    grace_candidate_ids = []
    try:
        async with AsyncSessionLocal() as read_session:
            offer_grace_stmt = select(Offer.id).where(
                Offer.status == "active",
                Offer.expires_at.is_not(None),
                Offer.expires_at <= now
            ).limit(100)
            offer_grace_res = await read_session.execute(offer_grace_stmt)
            grace_candidate_ids = list(offer_grace_res.scalars().all())
    except Exception as e:
        logger.error(f"Error querying candidate active offer IDs: {e}", exc_info=True)

    for offer_id in grace_candidate_ids:
        try:
            async with AsyncSessionLocal() as item_session:
                offer_res = await item_session.execute(
                    select(Offer).where(Offer.id == offer_id).with_for_update(skip_locked=True)
                )
                offer = offer_res.scalar_one_or_none()
                if not offer:
                    continue

                if not can_expire_active_offer(offer.status, offer.expires_at, now):
                    continue

                # Rule 1: Delete offer immediately when duration expires without applicants
                offer.status = "deleted"
                offer.status_changed_at = now
                offer_id_val = offer.id
                user_id_val = offer.user_id

                await item_session.commit()
                deleted_offers_count += 1
                logger.info(f"Offer #{offer_id_val} deleted automatically as duration expired at {now.isoformat()}")

                try:
                    await update_channel_offer_message(bot, offer)
                except Exception as e:
                    logger.warning(f"Failed to update channel message for deleted offer #{offer_id_val}: {e}")

                deleted_text = (
                    f"🗑️ **تم حذف عرضك رقم #{offer_id_val} تلقائياً**\n\n"
                    f"انتهت المدة المحددة لعرضك في السوق دون أن يتقدم أحد عليه، وتم حذفه من القناة والسوق نهائياً.\n"
                    f"يمكنك إنشاء عرض جديد في أي وقت عبر البوت."
                )
                try:
                    await bot.send_message(
                        user_id_val,
                        deleted_text,
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.warning(f"Failed to notify user {user_id_val} of deleted offer #{offer_id_val}: {e}")
        except Exception as item_err:
            logger.error(f"Error deleting offer #{offer_id}: {item_err}", exc_info=True)

    # ----------------------------------------------------
    # Branch 3: SYP Payment 30-Minute Reminder (Buyer Only, Deal Stays Open)
    # ----------------------------------------------------
    syp_reminder_ids = []
    try:
        async with AsyncSessionLocal() as read_session:
            syp_stmt = select(Deal.id).where(
                Deal.status == "AWAITING_SYP_PAYMENT",
                Deal.expires_at.is_not(None),
                Deal.expires_at <= now
            ).limit(100)
            syp_res = await read_session.execute(syp_stmt)
            syp_reminder_ids = list(syp_res.scalars().all())
    except Exception as e:
        logger.error(f"Error querying candidate SYP payment reminder deal IDs: {e}", exc_info=True)

    support_username = (settings.SUPPORT_USERNAME or "AlphaSupport").lstrip("@")
    for deal_id in syp_reminder_ids:
        try:
            async with AsyncSessionLocal() as item_session:
                deal_res = await item_session.execute(
                    select(Deal).where(Deal.id == deal_id).with_for_update(skip_locked=True)
                )
                deal = deal_res.scalar_one_or_none()
                if not deal:
                    continue

                if not can_remind_syp_payment(deal.status, deal.expires_at, now):
                    continue

                # Clear expires_at so the reminder sends strictly once while keeping the deal open
                deal.expires_at = None
                deal_id_val = deal.id
                buyer_id_val = deal.buyer_id
                amount_usdt_val = deal.amount_usdt
                amount_syp_val = deal.amount_syp

                await item_session.commit()
                syp_reminders_count += 1
                logger.info(f"Sent 30-min SYP payment reminder to buyer for deal #{deal_id_val}")

                if buyer_id_val:
                    reminder_kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="📸 تأكيد تحويل المبلغ وإرفاق الإشعار", callback_data=f"man_syp_{deal_id_val}")]
                    ])
                    reminder_text = (
                        f"⏰ **تذكير مهلة التحويل — صفقة #{deal_id_val}**\n\n"
                        f"▫️ **رصيد الضمان المحجوز:** `{format_usdt(amount_usdt_val)} USDT`\n"
                        f"▫️ **المبلغ المطلوب بالليرة:** `{format_syp(amount_syp_val)}` ل.س\n\n"
                        f"مرت 30 دقيقة على مرحلة بانتظار تحويل الليرة السورية، ولا تزال الصفقة قائمة والضمان محجوزاً بأمان.\n"
                        f"⚠️ **يرجى التواصل مع فريق الدعم الفني:** @{support_username}\n"
                        f"قبل تنفيذ التحويل، أو إرفاق إشعار التحويل أدناه إذا قمت بالتحويل بالفعل."
                    )
                    try:
                        await bot.send_message(
                            chat_id=buyer_id_val,
                            text=reminder_text,
                            reply_markup=reminder_kb,
                            parse_mode="Markdown"
                        )
                    except Exception as e:
                        logger.warning(f"Failed to notify buyer {buyer_id_val} of 30-min SYP reminder for deal #{deal_id_val}: {e}")
        except Exception as item_err:
            logger.error(f"Error sending SYP payment reminder for deal #{deal_id}: {item_err}", exc_info=True)

    # ----------------------------------------------------
    # Branch 4: Expired Negotiations (Max 100 per cycle)
    # ----------------------------------------------------
    neg_candidate_ids = []
    try:
        async with AsyncSessionLocal() as read_session:
            neg_expired_stmt = select(Negotiation.id).where(
                Negotiation.status.in_(["PENDING_MAKER", "PENDING_TAKER"]),
                Negotiation.expires_at <= now
            ).limit(100)
            neg_expired_res = await read_session.execute(neg_expired_stmt)
            neg_candidate_ids = list(neg_expired_res.scalars().all())
    except Exception as e:
        logger.error(f"Error querying candidate expired negotiation IDs: {e}", exc_info=True)

    for neg_id in neg_candidate_ids:
        try:
            async with AsyncSessionLocal() as item_session:
                neg_res = await item_session.execute(
                    select(Negotiation).where(Negotiation.id == neg_id).with_for_update(skip_locked=True)
                )
                neg = neg_res.scalar_one_or_none()
                if not neg:
                    continue

                if not can_expire_negotiation(neg.status, neg.expires_at, now):
                    continue

                neg_id_val = neg.id
                neg_offer_id_val = neg.offer_id
                taker_id_val = neg.taker_id
                maker_id_val = neg.maker_id

                revert_offer = False
                reverted_offer = None
                if neg_offer_id_val:
                    offer_res = await item_session.execute(
                        select(Offer).where(Offer.id == neg_offer_id_val).with_for_update()
                    )
                    offer = offer_res.scalar_one_or_none()
                    if offer and offer.status == "negotiating":
                        offer.status = "active"
                        offer.status_changed_at = now
                        revert_offer = True
                        reverted_offer = offer

                # Do not set status to EXPIRED until offer lock is acquired
                neg.status = "EXPIRED"

                await item_session.commit()
                expired_negs_count += 1
                logger.info(f"Negotiation #{neg_id_val} expired at {now.isoformat()}")

                if revert_offer and reverted_offer:
                    try:
                        await update_channel_offer_message(bot, reverted_offer)
                    except Exception as e:
                        logger.warning(f"Failed to update channel message for negotiation offer #{neg_offer_id_val}: {e}")

                try:
                    await bot.send_message(
                        taker_id_val,
                        f"⌛ **انتهت مهلة جلسة التفاوض — عرض #{neg_offer_id_val}**\n\n"
                        f"تم إنهاء الجلسة لعدم الرد خلال مهلة 30 دقيقة.\n"
                        f"تمت إعادة العرض إلى السوق كعرض نشط ومتاح.",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.warning(f"Failed to notify taker {taker_id_val} of negotiation expiry: {e}")

                try:
                    await bot.send_message(
                        maker_id_val,
                        f"⌛ **انتهت مهلة جلسة التفاوض — عرض #{neg_offer_id_val}**\n\n"
                        f"تم إنهاء الجلسة تلقائياً لانقضاء مهلة 30 دقيقة.\n"
                        f"تمت إعادة عرضك #{neg_offer_id_val} كعرض نشط ومتاح في السوق.",
                        parse_mode="Markdown"
                    )
                except Exception as e:
                    logger.warning(f"Failed to notify maker {maker_id_val} of negotiation expiry: {e}")
        except Exception as item_err:
            logger.error(f"Error expiring negotiation #{neg_id}: {item_err}", exc_info=True)

    # ----------------------------------------------------
    # Branch 5: Orphan Reserved/Negotiating Offers Safety Net (Max 100 per cycle)
    # ----------------------------------------------------
    orphan_released_count = 0
    orphan_candidate_ids = []
    try:
        async with AsyncSessionLocal() as read_session:
            orphan_stmt = select(Offer.id).where(
                Offer.status.in_(["reserved", "negotiating"])
            ).limit(100)
            orphan_res = await read_session.execute(orphan_stmt)
            orphan_candidate_ids = list(orphan_res.scalars().all())
    except Exception as e:
        logger.error(f"Error querying candidate orphan offer IDs: {e}", exc_info=True)

    for offer_id in orphan_candidate_ids:
        try:
            async with AsyncSessionLocal() as item_session:
                offer_res = await item_session.execute(
                    select(Offer).where(Offer.id == offer_id).with_for_update(skip_locked=True)
                )
                offer = offer_res.scalar_one_or_none()
                if not offer:
                    continue

                live_deal_res = await item_session.execute(
                    select(Deal.id).where(
                        Deal.offer_id == offer_id,
                        Deal.status.not_in(["COMPLETED", "CANCELLED", "EXPIRED"])
                    ).limit(1)
                )
                has_live_deal = live_deal_res.scalar_one_or_none() is not None

                live_neg_res = await item_session.execute(
                    select(Negotiation.id).where(
                        Negotiation.offer_id == offer_id,
                        Negotiation.status.in_(["PENDING_MAKER", "PENDING_TAKER"])
                    ).limit(1)
                )
                has_live_neg = live_neg_res.scalar_one_or_none() is not None

                if not should_release_orphan_offer(
                    offer.status,
                    has_live_deal,
                    has_live_neg,
                    offer.status_changed_at,
                    now
                ):
                    continue

                prev_status = offer.status
                offer.status = "active"
                offer.status_changed_at = now
                await item_session.commit()
                orphan_released_count += 1
                logger.info(f"Orphan offer #{offer.id} ({prev_status}) returned to active at {now.isoformat()}")

                try:
                    await update_channel_offer_message(bot, offer)
                except Exception as e:
                    logger.warning(f"Failed to update channel message for released orphan offer #{offer.id}: {e}")
        except Exception as item_err:
            logger.error(f"Error releasing orphan offer #{offer_id}: {item_err}", exc_info=True)

    return expired_deals_count, deleted_offers_count, syp_reminders_count, expired_negs_count, orphan_released_count


async def run_startup_recovery(bot: Bot):
    """
    Executed immediately upon bot boot/reboot:
    Audits and synchronizes any deals, negotiations, and offers that reached their timeout
    while the server was offline or during a restart.
    """
    logger.info("Initiating startup recovery audit for pending timers and expiries...")
    try:
        counts = await process_expiries(bot)
        expired_deals, deleted_offers, syp_reminders, expired_negs, orphan_offers = counts
        logger.info(
            f"Startup recovery completed successfully: "
            f"{expired_deals} expired deals, {deleted_offers} deleted offers, "
            f"{syp_reminders} SYP reminders sent, {expired_negs} expired negotiations, "
            f"{orphan_offers} orphan offers released."
        )
    except Exception as e:
        logger.critical(f"Startup recovery encountered an error: {e}", exc_info=True)

async def start_expiry_watcher(bot: Bot, interval_seconds: int = 30):
    """
    Unified resilient background worker loop checking for expired deals, negotiations,
    and offers periodically (every 30s) with supervisor recovery.
    """
    logger.info("Unified expiry background watcher started.")
    
    while True:
        try:
            # Run startup recovery scan
            await run_startup_recovery(bot)
            
            while True:
                try:
                    await process_expiries(bot)
                except asyncio.CancelledError:
                    raise
                except Exception as loop_err:
                    logger.error(f"Error inside expiry cycle: {loop_err}", exc_info=True)
                await asyncio.sleep(interval_seconds)

        except asyncio.CancelledError:
            logger.info("Unified expiry background watcher cancelled.")
            break
        except Exception as crash_err:
            logger.critical(f"Supervisor: Expiry watcher crashed, restarting in 10s: {crash_err}", exc_info=True)
            await asyncio.sleep(10)
