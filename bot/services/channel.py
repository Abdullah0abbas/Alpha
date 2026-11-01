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
        
    offer_type_ar = "🔴 بيع USDT" if offer.offer_type == 'sell' else "🟢 شراء USDT"
    action_ar = "شراء" if offer.offer_type == 'sell' else "بيع"
    
    methods_str = format_payment_methods(offer.payment_methods)
    rate_str = format_syp(offer.rate)
    min_str = format_usdt(offer.min_limit)
    max_str = format_usdt(offer.max_limit)
    fee_line = format_fee_rule_html()
    
    if offer.status == "active":
        status_header = f"\u200f📢 <b>{CHANNEL_NAME} | عرض تداول #{offer.id} — {offer_type_ar}</b>"
        details_text = (
            f"━━━━━━━━━━━━━━━\n"
            f"• <b>سعر الصرف:</b> 💵 <code>{rate_str} SYP</code>\n"
            f"• <b>حدود الكمية:</b> 📦 <code>{min_str}</code> — <code>{max_str}</code> USDT\n"
            f"• <b>وسائل الدفع:</b> 💳 {methods_str}\n"
            f"• {fee_line}\n"
            f"━━━━━━━━━━━━━━━"
        )
        deep_link = f"https://t.me/{settings.BOT_USERNAME}?start=offer_{offer.id}"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⚡ بدء تنفيذ العرض ({action_ar})", url=deep_link)]
        ])
        channel_text = f"{status_header}\n\n{details_text}"
    else:
        # Strike through all offer details when reserved, completed, expired, or cancelled
        details_text = (
            f"━━━━━━━━━━━━━━━\n"
            f"<s>• النوع: {offer_type_ar}</s>\n"
            f"<s>• السعر: 💵 {rate_str} SYP</s>\n"
            f"<s>• الحدود: 📦 {min_str} — {max_str} USDT</s>\n"
            f"<s>• الدفع: 💳 {methods_str}</s>\n"
            f"━━━━━━━━━━━━━━━"
        )
        deep_link = f"https://t.me/{settings.BOT_USERNAME}?start=offers"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 تصفح العروض المتاحة", url=deep_link)]
        ])

        if offer.status == "reserved":
            status_header = f"\u200f🔒 <b>{CHANNEL_NAME} | عرض #{offer.id} [محجوز لصفقة]</b>"
            action_section = "⏳ <b>الحالة:</b> قيد التنفيذ والتسوية."
        elif offer.status == "negotiating":
            status_header = f"\u200f🤝 <b>{CHANNEL_NAME} | عرض #{offer.id} [قيد التفاوض]</b>"
            action_section = "💬 <b>الحالة:</b> تجري مفاوضات حالياً على هذا العرض."
        elif offer.status == "completed":
            status_header = f"\u200f✅ <b>{CHANNEL_NAME} | عرض #{offer.id} [مكتمل ومغلق]</b>"
            action_section = "🎉 <b>الحالة:</b> تم تنفيذ وإغلاق الصفقة بنجاح."
        elif offer.status in ["expired", "expired_grace"]:
            status_header = f"\u200f⏱️ <b>{CHANNEL_NAME} | عرض #{offer.id} [منتهي الصلاحية]</b>"
            action_section = "⌛ <b>الحالة:</b> انتهت مهلة وصلاحية العرض."
        elif offer.status in ["cancelled", "deleted"]:
            status_header = f"\u200f🗑️ <b>{CHANNEL_NAME} | عرض #{offer.id} [تم الحذف]</b>"
            action_section = "🚫 <b>الحالة:</b> تم حذف العرض من القناة والسوق."
        else:
            status_header = f"\u200fℹ️ <b>{CHANNEL_NAME} | عرض #{offer.id}</b>"
            action_section = f"📊 <b>الحالة:</b> {offer.status}"
            
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
