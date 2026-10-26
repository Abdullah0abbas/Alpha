from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from aiogram.types import InlineKeyboardButton

from db.models import Deal, User
from bot.utils.formatters import format_syp, format_usdt, safe_username, escape_markdown, format_local_datetime


def build_offer_action_buttons(
    offer_id: int,
    offer_user_id: int,
    viewer_id: int,
    negotiable: bool,
    negotiable_type: str = "none",
    context: str = "offers"
) -> list[list[InlineKeyboardButton]]:
    """
    Pure builder for offer action buttons ('start_deal' / 'start_neg') across /offers, paged browse, and start deeplinks.
    Strictly omits deal/negotiation action buttons if viewer_id is the offer creator.
    """
    if offer_user_id == viewer_id:
        if context == "start":
            return [[InlineKeyboardButton(text="🔙 العودة للرئيسية", callback_data="menu_back_main")]]
        return []

    buttons = []
    if context == "browse":
        row = [InlineKeyboardButton(text=f"⚡ صفقة #{offer_id}", callback_data=f"start_deal_{offer_id}")]
        if negotiable and negotiable_type != "none":
            row.append(InlineKeyboardButton(text=f"🤝 تفاوض #{offer_id}", callback_data=f"start_neg_{offer_id}"))
        return [row]

    if context == "start":
        buttons.append([InlineKeyboardButton(text="⚡ تنفيذ الصفقة فوراً", callback_data=f"start_deal_{offer_id}")])
        if negotiable and negotiable_type != "none":
            buttons.append([InlineKeyboardButton(text="🤝 تقديم طلب تفاوض", callback_data=f"start_neg_{offer_id}")])
        buttons.append([InlineKeyboardButton(text="🔙 العودة للرئيسية", callback_data="menu_back_main")])
        return buttons

    # Default context == "offers"
    buttons.append([InlineKeyboardButton(text="⚡ تنفيذ فوري بالسعر الحالي", callback_data=f"start_deal_{offer_id}")])
    if negotiable and negotiable_type != "none":
        buttons.append([InlineKeyboardButton(text="🤝 تقديم طلب تفاوض", callback_data=f"start_neg_{offer_id}")])
    return buttons

async def format_admin_deal_review_text(deal: Deal, db_session: AsyncSession) -> str:
    """Format rich, detailed notification text for platform administrators."""
    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)
    fee_str = format_usdt(deal.fee_usdt)
    created_date_str = format_local_datetime(deal.created_at) if deal.created_at else "غير محدد"

    # Calculate effective exchange rate
    if deal.amount_usdt and deal.amount_usdt > 0:
        rate = (deal.amount_syp / deal.amount_usdt).quantize(Decimal("1"))
        rate_str = format_syp(rate)
    else:
        rate_str = "غير محدد"

    # Fetch detailed user records
    seller_user_res = await db_session.execute(select(User).where(User.telegram_id == deal.seller_id))
    seller_user = seller_user_res.scalar_one_or_none()
    seller_display = safe_username(seller_user.username if seller_user else None, deal.seller_id)
    seller_deals_count = seller_user.completed_deals_count if seller_user else 0
    seller_rep = f"{seller_user.reputation_score}%" if (seller_user and seller_user.reputation_score is not None) else "100%"

    buyer_user_res = await db_session.execute(select(User).where(User.telegram_id == deal.buyer_id))
    buyer_user = buyer_user_res.scalar_one_or_none()
    buyer_display = safe_username(buyer_user.username if buyer_user else None, deal.buyer_id)
    buyer_deals_count = buyer_user.completed_deals_count if buyer_user else 0
    buyer_rep = f"{buyer_user.reputation_score}%" if (buyer_user and buyer_user.reputation_score is not None) else "100%"

    # Format seller payment info
    if isinstance(deal.seller_payment_info, dict):
        seller_account_info = deal.seller_payment_info.get("info", "غير محدد")
    else:
        seller_account_info = str(deal.seller_payment_info or "غير محدد")

    seller_account_escaped = escape_markdown(seller_account_info)
    buyer_wallet_escaped = escape_markdown(deal.buyer_payout_address or "غير محدد")

    return (
        f"🛡️ **مراجعة وتدقيق صفقة ضمان #{deal.id}**\n\n\n"
        f"**تفاصيل الصفقة المالية:**\n\n"
        f"• وقت الإنشاء:  `{created_date_str}`\n"
        f"• الكمية:  `{usdt_str}` USDT\n"
        f"• الإجمالي:  `{syp_str}` SYP\n"
        f"• سعر الصرف:  `{rate_str}` SYP\n"
        f"• عمولة الضمان:  `{fee_str}` USDT\n"
        f"• محفظة الإيداع:  `{deal.deposit_address}`\n\n\n"
        f"🔴 **طرف البيع:**\n\n"
        f"• المعرف:  `{deal.seller_id}` ({seller_display})\n"
        f"• السجل:  `{seller_deals_count}` صفقة ({seller_rep})\n"
        f"• حساب استلام الليرة:  `{seller_account_escaped}`\n\n\n"
        f"🟢 **طرف الشراء:**\n\n"
        f"• المعرف:  `{deal.buyer_id}` ({buyer_display})\n"
        f"• السجل:  `{buyer_deals_count}` صفقة ({buyer_rep})\n"
        f"• محفظة استلام USDT:  `{buyer_wallet_escaped}`\n\n\n"
        f"**سجل التحقق:**\n\n"
        f"• إيداع الضمان: مؤكد ومحجوز ✅\n"
        f"• تحويل الليرة: مؤكد استلامه من البائع ✅\n"
        f"• عنوان المستلم: مسجل ومتحقق منه ✅\n\n\n"
        f"الصفقة جاهزة للاعتماد النهائي وإطلاق الرصيد:"
    )
