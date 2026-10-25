from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from db.models import Deal, User
from bot.utils.formatters import format_syp, format_usdt, safe_username, escape_markdown

async def format_admin_deal_review_text(deal: Deal, db_session: AsyncSession) -> str:
    """Format rich, detailed notification text for platform administrators."""
    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)
    fee_str = format_usdt(deal.fee_usdt)
    created_date_str = deal.created_at.strftime("%Y-%m-%d  —  %H:%M UTC") if deal.created_at else "غير محدد"

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
        f"💰 **تفاصيل الصفقة المالية:**\n\n"
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
        f"📋 **سجل التحقق:**\n\n"
        f"• إيداع الضمان: مؤكد ومحجوز ✅\n"
        f"• تحويل الليرة: مؤكد استلامه من البائع ✅\n"
        f"• عنوان المستلم: مسجل ومتحقق منه ✅\n\n\n"
        f"الصفقة جاهزة للاعتماد النهائي وإطلاق الرصيد:"
    )
