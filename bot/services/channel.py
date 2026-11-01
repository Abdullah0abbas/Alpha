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
    format_usdt
)

logger = logging.getLogger(__name__)

async def update_channel_offer_message(bot: Bot, offer: Offer) -> None:
    """Updates the public Telegram channel broadcast message for an offer based on its status with premium UI/UX formatting."""
    if not settings.CHANNEL_ID or not settings.BOT_USERNAME or not offer.channel_message_id:
        return
        
    offer_type_ar = "🔴 التاجر يريد بيع \"USDT\"" if offer.offer_type == 'sell' else "🟢 التاجر يريد شراء \"USDT\""
    action_ar = "💸 شراء هذا العرض" if offer.offer_type == 'sell' else "💸 البيع لهذا الزبون"
    
    methods_str = format_payment_methods(offer.payment_methods)
    rate_str = format_syp(offer.rate)
    min_str = format_usdt(offer.min_limit)
    max_str = format_usdt(offer.max_limit)
    fee_line = format_fee_rule_html()
    dur_str = f"{int(offer.duration_hours)} ساعات" if (offer.duration_hours and offer.duration_hours > 0) else "4 ساعات"
    
    if offer.status == "active":
        status_header = f"<b>{CHANNEL_NAME}</b>\n\nفرصة رقم : #{offer.id}\n{offer_type_ar}"
        details_text = (
            f"~~~~~~~~~~~~~~~~\n"
            f"💰 <b>الكمية :</b> من {min_str} إلى {max_str} USDT\n"
            f"💵 <b>سعر الصرف :</b> {rate_str} ل.س\n"
            f"💳 <b>طرق الدفع :</b> {methods_str}\n"
            f"⌛ <b>مدة المعاملة :</b> {dur_str}\n"
            f"~~~~~~~~~~~~~~~~\n"
            f"{fee_line}"
        )
        deep_link = f"https://t.me/{settings.BOT_USERNAME}?start=offer_{offer.id}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=action_ar, url=deep_link),
                InlineKeyboardButton(text="🔍 تصفح عروض أخرى", url=f"https://t.me/{settings.BOT_USERNAME}?start=offers")
            ]
        ])
        channel_text = f"{status_header}\n\n{details_text}"
    else:
        # Strike through all offer details when reserved, completed, expired, or cancelled
        details_text = (
            f"~~~~~~~~~~~~~~~~\n"
            f"<s>💰 الكمية : من {min_str} إلى {max_str} USDT</s>\n"
            f"<s>💵 سعر الصرف : {rate_str} ل.س</s>\n"
            f"<s>💳 طرق الدفع : {methods_str}</s>\n"
            f"~~~~~~~~~~~~~~~~"
        )
        deep_link = f"https://t.me/{settings.BOT_USERNAME}?start=offers"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 تصفح العروض المتاحة", url=deep_link)]
        ])

        if offer.status == "reserved":
            status_header = f"🔒 <b>{CHANNEL_NAME} | فرصة #{offer.id} [محجوزة لصفقة]</b>"
            action_section = "⏳ <b>الحالة :</b> قيد التنفيذ والتسوية."
        elif offer.status == "negotiating":
            status_header = f"🤝 <b>{CHANNEL_NAME} | فرصة #{offer.id} [قيد التفاوض]</b>"
            action_section = "💬 <b>الحالة :</b> تجري مفاوضات حالياً على هذا العرض."
        elif offer.status == "completed":
            status_header = f"✅ <b>{CHANNEL_NAME} | فرصة #{offer.id} [مكتملة]</b>"
            action_section = "🎉 <b>الحالة :</b> تم تنفيذ وإغلاق الصفقة بنجاح."
        elif offer.status in ["expired", "expired_grace"]:
            status_header = f"⏱️ <b>{CHANNEL_NAME} | فرصة #{offer.id} [منتهية]</b>"
            action_section = "⌛ <b>الحالة :</b> انتهت مهلة وصلاحية العرض."
        elif offer.status in ["cancelled", "deleted"]:
            status_header = f"🗑️ <b>{CHANNEL_NAME} | فرصة #{offer.id} [ملغاة]</b>"
            action_section = "🚫 <b>الحالة :</b> تم حذف أو إلغاء العرض."
        else:
            status_header = f"ℹ️ <b>{CHANNEL_NAME} | فرصة #{offer.id}</b>"
            action_section = f"📊 <b>الحالة :</b> {offer.status}"
            
        channel_text = f"{status_header}\n\n{details_text}\n\n{action_section}"
    
    if offer.status == "deleted":
        try:
            await bot.delete_message(chat_id=settings.CHANNEL_ID, message_id=offer.channel_message_id)
            return
        except Exception as delete_err:
            logger.warning(f"Failed to delete channel message for offer {offer.id}, falling back to edit: {delete_err}")

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
