import logging
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from db.models import Offer
from config.settings import settings
from bot.utils.branding import CHANNEL_NAME
from bot.utils.fees import format_fee_rule_html
from bot.utils.formatters import (
    format_payment_methods,
    format_syp,
    format_usdt,
    humanize_time_remaining,
)

logger = logging.getLogger(__name__)

def build_channel_offer_message(
    offer: Offer,
    duration_override: str | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """
    Builds clean, formal HTML message text and inline keyboard for public channel offer posts.
    Reused across initial broadcast and status updates so formatting never drifts.
    """
    offer_type_ar = "🔴 بيع (المعلن يبيع)" if offer.offer_type == "sell" else "🟢 شراء (المعلن يشتري)"
    action_ar = "🤝 تنفيذ هذا العرض"

    methods_str = format_payment_methods(offer.payment_methods)
    rate_str = format_syp(offer.rate)
    min_str = format_usdt(offer.min_limit)
    max_str = format_usdt(offer.max_limit)
    fee_line = format_fee_rule_html()

    if duration_override:
        dur_str = duration_override
    else:
        raw_duration = getattr(offer, "duration_hours", None)
        if raw_duration and raw_duration > 0:
            hrs = int(raw_duration) if float(raw_duration).is_integer() else raw_duration
            dur_str = f"{hrs} ساعات"
        else:
            dur_str = humanize_time_remaining(getattr(offer, "expires_at", None), "4 ساعات")

    neg_labels = {
        "amount": "متاح على الكمية",
        "rate": "متاح على السعر",
        "both": "متاح على السعر والكمية",
    }
    if getattr(offer, "negotiable", False):
        neg_text = neg_labels.get(getattr(offer, "negotiable_type", "none"), "متاح")
        neg_line = f"\n▫️ <b>التفاوض:</b> {neg_text}"
    else:
        neg_line = ""

    if offer.status == "active":
        channel_text = (
            f"📢 <b>{CHANNEL_NAME} | عرض #{offer.id}</b>\n"
            f"▫️ <b>الحالة:</b> متاح للتداول\n\n"
            f"▫️ <b>نوع العرض:</b> {offer_type_ar}\n"
            f"▫️ <b>سعر الصرف:</b> {rate_str} ل.س\n"
            f"▫️ <b>حدود الكمية:</b> من {min_str} إلى {max_str} دولار رقمي\n"
            f"▫️ <b>وسائل الدفع:</b> {methods_str}{neg_line}\n"
            f"▫️ <b>الصلاحية المتبقية:</b> {dur_str}\n\n"
            f"{fee_line}"
        )
        deep_link = f"https://t.me/{settings.BOT_USERNAME}?start=offer_{offer.id}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=action_ar, url=deep_link),
                InlineKeyboardButton(text="🔍 تصفح العروض", url=f"https://t.me/{settings.BOT_USERNAME}?start=offers"),
            ]
        ])
        return channel_text, kb

    details_text = (
        f"<s>▫️ نوع العرض: {offer_type_ar}</s>\n"
        f"<s>▫️ سعر الصرف: {rate_str} ل.س</s>\n"
        f"<s>▫️ حدود الكمية: من {min_str} إلى {max_str} دولار رقمي</s>\n"
        f"<s>▫️ وسائل الدفع: {methods_str}</s>"
    )
    deep_link = f"https://t.me/{settings.BOT_USERNAME}?start=offers"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 تصفح العروض المتاحة", url=deep_link)]
    ])

    if offer.status == "reserved":
        status_header = f"🔒 <b>{CHANNEL_NAME} | عرض #{offer.id}</b>"
        action_section = "⏳ <b>الحالة:</b> العرض محجوز حالياً — قيد التنفيذ والتسوية."
    elif offer.status == "negotiating":
        status_header = f"🤝 <b>{CHANNEL_NAME} | عرض #{offer.id}</b>"
        action_section = "💬 <b>الحالة:</b> تجري مفاوضات حالياً على هذا العرض."
    elif offer.status == "completed":
        status_header = f"✅ <b>{CHANNEL_NAME} | عرض #{offer.id}</b>"
        action_section = "☑️ <b>الحالة:</b> تم تنفيذ الصفقة وإغلاق العرض بنجاح."
    elif offer.status == "expired":
        status_header = f"⏱️ <b>{CHANNEL_NAME} | عرض #{offer.id}</b>"
        action_section = "⌛ <b>الحالة:</b> انتهت صلاحية العرض وتم إغلاقه."
    elif offer.status in ["cancelled", "deleted"]:
        status_header = f"🚫 <b>{CHANNEL_NAME} | عرض #{offer.id}</b>"
        action_section = "🗑️ <b>الحالة:</b> تم إلغاء العرض وإزالته من السوق."
    else:
        status_header = f"ℹ️ <b>{CHANNEL_NAME} | عرض #{offer.id}</b>"
        action_section = f"📊 <b>الحالة:</b> {offer.status}"

    channel_text = f"{status_header}\n\n{details_text}\n\n{action_section}"
    return channel_text, kb


async def update_channel_offer_message(bot: Bot, offer: Offer) -> None:
    """
    Updates the public Telegram channel broadcast message for an offer based on its status.
    """
    if not settings.CHANNEL_ID or not settings.BOT_USERNAME or not offer.channel_message_id:
        return

    if offer.status == "deleted":
        try:
            await bot.delete_message(chat_id=settings.CHANNEL_ID, message_id=offer.channel_message_id)
            return
        except Exception as delete_err:
            logger.warning(f"Failed to delete channel message for offer {offer.id}, falling back to edit: {delete_err}")

    channel_text, kb = build_channel_offer_message(offer)

    try:
        await bot.edit_message_text(
            chat_id=settings.CHANNEL_ID,
            message_id=offer.channel_message_id,
            text=channel_text,
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.warning(f"Failed to edit channel message for offer {offer.id}: {e}")

