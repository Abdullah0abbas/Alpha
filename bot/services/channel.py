import html
import logging
from decimal import Decimal, ROUND_HALF_UP
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy.future import select
from db.models import Offer, User
from db.session import AsyncSessionLocal
from config.settings import settings
from bot.utils.formatters import (
    format_payment_methods,
    format_syp,
    format_usdt,
    humanize_time_remaining,
)

logger = logging.getLogger(__name__)


def _resolve_trader_stats(offer: Offer, trader=None) -> tuple[str, int, str, str, str]:
    user_obj = trader or getattr(offer, "trader", None) or getattr(offer, "user", None)
    completed = int(
        getattr(user_obj, "completed_deals_count", None)
        or getattr(user_obj, "completed_deals", None)
        or 0
    ) if user_obj else 0
    rep_raw = getattr(user_obj, "reputation_score", Decimal("100.0")) if user_obj else Decimal("100.0")
    try:
        rep_dec = rep_raw if isinstance(rep_raw, Decimal) else Decimal(str(rep_raw))
    except Exception:
        rep_dec = Decimal("100.0")

    tier_raw = getattr(user_obj, "tier", None) if user_obj else None
    if tier_raw == "Gold" or completed >= 50:
        tier_label = "ذهبي🥇"
    elif tier_raw == "Silver" or completed >= 10:
        tier_label = "فضي🥈"
    else:
        tier_label = "برونزي🥉"

    kyc_raw = getattr(user_obj, "kyc_status", "unverified") if user_obj else "unverified"
    kyc_label = "موثق ✅" if kyc_raw == "verified" else "غير موثق"

    stars_5 = ((rep_dec / Decimal("100")) * Decimal("5")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    rep_str = f"⭐️ {stars_5}"
    rate_pct = f"{rep_dec.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"
    return tier_label, completed, rate_pct, rep_str, kyc_label


def format_trader_trust_markdown(trader) -> str:
    """
    Formats a single concise Markdown line for in-bot offer views (Market & Deep-Link & /offers):
    e.g., 'موثق ✅ | ⭐ `100.0%` (`12` صفقات)' or 'غير موثق | ⭐ `100.0%` (`0` صفقات)'
    """
    completed = int(
        getattr(trader, "completed_deals_count", None)
        or getattr(trader, "completed_deals", None)
        or 0
    ) if trader else 0
    rep_raw = getattr(trader, "reputation_score", Decimal("100.0")) if trader else Decimal("100.0")
    try:
        rep_dec = rep_raw if isinstance(rep_raw, Decimal) else Decimal(str(rep_raw))
    except Exception:
        rep_dec = Decimal("100.0")
    rate_pct = f"{rep_dec.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"
    kyc_raw = getattr(trader, "kyc_status", "unverified") if trader else "unverified"
    kyc_badge = "موثق ✅" if kyc_raw == "verified" else "غير موثق"
    return f"{kyc_badge} | ⭐ `{rate_pct}` (`{completed}` صفقات)"


def build_channel_offer_message(
    offer: Offer,
    duration_override: str | None = None,
    trader=None,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Builds clean, formal HTML message text and inline keyboard for public channel offer posts.
    Reused across initial broadcast and status updates so formatting never drifts.
    """
    is_sell = offer.offer_type == "sell"
    header_action = '🔴 التاجر يريد بيع "USDT"' if is_sell else '🟢 التاجر يريد شراء "USDT"'

    methods_str = format_payment_methods(offer.payment_methods)
    rate_str = format_syp(offer.rate)
    min_str = format_usdt(offer.min_limit)
    max_str = format_usdt(offer.max_limit)

    if duration_override:
        dur_str = duration_override
    else:
        raw_duration = getattr(offer, "duration_hours", None)
        if raw_duration and raw_duration > 0:
            from bot.utils.offer_wizard import format_duration_hours_minutes
            dur_str = format_duration_hours_minutes(raw_duration)
        else:
            dur_str = humanize_time_remaining(getattr(offer, "expires_at", None), "4 ساعات")

    neg_labels = {
        "amount": "متاح على الكمية",
        "rate": "متاح على السعر",
        "both": "متاح على السعر والكمية",
    }
    if getattr(offer, "negotiable", False):
        neg_text = neg_labels.get(getattr(offer, "negotiable_type", "none"), "متاح")
        neg_line = f"\n🤝 التفاوض : {neg_text}"
    else:
        neg_line = ""

    raw_note = getattr(offer, "note", None)
    if raw_note:
        note_line = f"\n📝 الملاحظة : {html.escape(str(raw_note))}"
    else:
        note_line = ""

    tier_label, completed_deals, rate_pct, rep_str, kyc_label = _resolve_trader_stats(offer, trader=trader)

    core_card = (
        f"فرصة رقم : {offer.id} (عرض #{offer.id})\n"
        f"{header_action}\n"
        f"〰️〰️〰️〰️〰️〰️〰️\n"
        f"💰 الكمية : من <code>{min_str}</code> إلى <code>{max_str} USDT</code>\n"
        f"📊 سعر الصرف : {rate_str} ل.س\n"
        f"🏦 طرق الدفع : {methods_str}{neg_line}{note_line}\n"
        f"⏳ مدة المعاملة : {dur_str} (الصلاحية المتبقية)\n"
        f"〰️〰️〰️〰️〰️〰️〰️〰️〰️\n"
        f"التاجر :\n"
        f"🆔 التوثيق : {kyc_label}\n"
        f"👤 المستوى : {tier_label}\n"
        f"📈 نسبة الإتمام: {rate_pct} ({completed_deals} صفقات)\n"
        f"🧐 السمعة : {rep_str}\n"
        f"📉 عمولة الوسيط: مجانية ≤ 25$ | 0.1% لما فوق"
    )

    if offer.status == "active":
        deep_link = f"https://t.me/{settings.BOT_USERNAME}?start=offer_{offer.id}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="⚡ تنفيذ العرض", url=deep_link),
                InlineKeyboardButton(text="📊 السوق", url=f"https://t.me/{settings.BOT_USERNAME}?start=offers"),
            ]
        ])
        return core_card, kb

    deep_link = f"https://t.me/{settings.BOT_USERNAME}?start=offers"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 العروض المتاحة", url=deep_link)]
    ])

    if offer.status == "reserved":
        action_section = "🔒 <b>الحالة:</b> العرض محجوز حالياً — قيد التنفيذ."
    elif offer.status == "negotiating":
        action_section = "🤝 <b>الحالة:</b> تجري مفاوضات حالياً على هذا العرض."
    elif offer.status == "completed":
        action_section = "✅ <b>الحالة:</b> تم إتمام الصفقة وإغلاق العرض بنجاح."
    elif offer.status == "expired":
        action_section = "⏱️ <b>الحالة:</b> انتهت صلاحية العرض."
    elif offer.status in ["cancelled", "deleted"]:
        action_section = "🚫 <b>الحالة:</b> تم إلغاء العرض."
    else:
        action_section = f"ℹ️ <b>الحالة:</b> {offer.status}"

    channel_text = f"<s>{core_card}</s>\n\n{action_section}"
    return channel_text, kb


async def update_channel_offer_message(bot: Bot, offer: Offer, trader=None) -> None:
    """
    Updates the public Telegram channel broadcast message for an offer based on its status.
    Automatically loads the trader User record if not passed so KYC badge and reputation never reset.
    """
    if not settings.CHANNEL_ID or not settings.BOT_USERNAME or not offer.channel_message_id:
        return

    if offer.status == "deleted":
        try:
            await bot.delete_message(chat_id=settings.CHANNEL_ID, message_id=offer.channel_message_id)
            return
        except Exception as delete_err:
            err_msg = str(delete_err).lower()
            if "message to delete not found" in err_msg:
                return
            # Telegram prohibits bots from deleting messages older than 48 hours; fall back cleanly to striking through the post
            logger.info(f"Channel message for offer #{offer.id} cannot be deleted ({delete_err}); falling back to strike-through edit.")

    resolved_trader = trader
    if resolved_trader is None and getattr(offer, "user_id", None):
        try:
            async with AsyncSessionLocal() as session:
                res = await session.execute(select(User).where(User.telegram_id == offer.user_id))
                resolved_trader = res.scalar_one_or_none()
        except Exception as e:
            logger.warning(f"Failed to load trader {offer.user_id} for channel offer #{offer.id}: {e}")

    channel_text, kb = build_channel_offer_message(offer, trader=resolved_trader)

    try:
        await bot.edit_message_text(
            chat_id=settings.CHANNEL_ID,
            message_id=offer.channel_message_id,
            text=channel_text,
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        err_str = str(e).lower()
        if "message is not modified" not in err_str and "message to edit not found" not in err_str:
            logger.warning(f"Failed to edit channel message for offer {offer.id}: {e}")
