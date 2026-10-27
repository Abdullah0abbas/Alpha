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

STATUS_ARABIC_MAP = {
    "PENDING_OWNER_APPROVAL": "بانتظار موافقة صاحب العرض",
    "CREATED": "بانتظار بدء إيداع الضمان",
    "AWAITING_USDT_DEPOSIT": "بانتظار إيداع الضمان",
    "USDT_DEPOSITED": "تم إيداع الضمان بنجاح",
    "AWAITING_SYP_PAYMENT": "بانتظار تحويل الليرة",
    "SYP_PAYMENT_SENT": "تم إرسال الليرة وبانتظار التأكيد",
    "SYP_PAYMENT_CONFIRMED": "تم تأكيد استلام الليرة",
    "ADMIN_APPROVAL": "مراجعة واعتماد الإدارة",
    "RELEASING": "جاري إطلاق الرصيد",
    "COMPLETED": "مكتملة بنجاح",
    "CANCELLED": "ملغاة",
    "EXPIRED": "منتهية الصلاحية",
    "DISPUTED": "نزاع مفتوح",
}

async def format_admin_deal_review_text(deal: Deal, db_session: AsyncSession) -> str:
    """Format rich, detailed notification text for platform administrators."""
    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)
    created_date_str = format_local_datetime(deal.created_at) if deal.created_at else "غير محدد"

    # Fee breakdown
    seller_fee = getattr(deal, "seller_fee_usdt", None)
    buyer_fee = getattr(deal, "buyer_fee_usdt", None)
    if seller_fee is None or buyer_fee is None:
        from bot.utils.fees import calculate_deal_fee
        breakdown = calculate_deal_fee(deal.amount_usdt)
        seller_fee = breakdown.seller_fee
        buyer_fee = breakdown.buyer_fee
        total_fee = breakdown.total_fee
    else:
        total_fee = getattr(deal, "fee_usdt", seller_fee + buyer_fee)

    seller_fee_str = format_usdt(seller_fee)
    buyer_fee_str = format_usdt(buyer_fee)
    fee_str = format_usdt(total_fee)
    seller_deposit_str = format_usdt(deal.amount_usdt + seller_fee)
    buyer_receives_str = format_usdt(deal.amount_usdt - buyer_fee)

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

    # Honest verification block based on actual deal status
    current_status = deal.status
    status_label = STATUS_ARABIC_MAP.get(current_status, current_status)

    if current_status in ("ADMIN_APPROVAL", "SYP_PAYMENT_CONFIRMED"):
        verification_lines = (
            f"**سجل التحقق:**\n\n"
            f"• إيداع الضمان: مؤكد ومحجوز ✅\n"
            f"• تحويل الليرة: مؤكد استلامه من البائع ✅\n"
            f"• عنوان المستلم: مسجل ومتحقق منه ✅\n\n\n"
            f"الصفقة جاهزة للاعتماد النهائي وإطلاق الرصيد:"
        )
    elif current_status == "COMPLETED":
        verification_lines = (
            f"**سجل التحقق:**\n\n"
            f"• إيداع الضمان: مكتمل وتم إطلاقه ✅\n"
            f"• تحويل الليرة: مؤكد ومكتمل ✅\n"
            f"• عنوان المستلم: تم تحويل الرصيد إليه ✅\n\n\n"
            f"الصفقة مكتملة ومغلقة بنجاح."
        )
    elif current_status in ("PENDING_OWNER_APPROVAL", "CREATED", "AWAITING_USDT_DEPOSIT"):
        payout_status = "مسجل ومتحقق منه ✅" if (deal.buyer_payout_address and deal.buyer_payout_address != "PENDING") else "غير مسجل بعد ⏳"
        verification_lines = (
            f"**سجل التحقق (الوضع الحالي: {status_label}):**\n\n"
            f"• إيداع الضمان: بانتظار إيداع البائع ⏳\n"
            f"• تحويل الليرة: لم يبدأ بعد ⏳\n"
            f"• عنوان المستلم: {payout_status}\n\n\n"
            f"الصفقة قيد انتظار إيداع الضمان."
        )
    elif current_status in ("USDT_DEPOSITED", "AWAITING_SYP_PAYMENT"):
        payout_status = "مسجل ومتحقق منه ✅" if (deal.buyer_payout_address and deal.buyer_payout_address != "PENDING") else "غير مسجل بعد ⏳"
        verification_lines = (
            f"**سجل التحقق (الوضع الحالي: {status_label}):**\n\n"
            f"• إيداع الضمان: مؤكد ومحجوز ✅\n"
            f"• تحويل الليرة: بانتظار تحويل المشتري ⏳\n"
            f"• عنوان المستلم: {payout_status}\n\n\n"
            f"الصفقة قيد انتظار تحويل الليرة السورية."
        )
    elif current_status == "SYP_PAYMENT_SENT":
        payout_status = "مسجل ومتحقق منه ✅" if (deal.buyer_payout_address and deal.buyer_payout_address != "PENDING") else "غير مسجل بعد ⏳"
        verification_lines = (
            f"**سجل التحقق (الوضع الحالي: {status_label}):**\n\n"
            f"• إيداع الضمان: مؤكد ومحجوز ✅\n"
            f"• تحويل الليرة: أرسل المشتري الإشعار وبانتظار تأكيد البائع ⏳\n"
            f"• عنوان المستلم: {payout_status}\n\n\n"
            f"الصفقة بانتظار تأكيد البائع لاستلام الليرة."
        )
    elif current_status == "DISPUTED":
        verification_lines = (
            f"**سجل التحقق (الوضع الحالي: ⚠️ نزاع مفتوح):**\n\n"
            f"• إيداع الضمان: محجوز بأمان لدى المنصة 🛡️\n"
            f"• تحويل الليرة: محل نزاع بين الطرفين ⚠️\n"
            f"• عنوان المستلم: مسجل `{buyer_wallet_escaped}`\n\n\n"
            f"الصفقة تتطلب مراجعة النزاع واتخاذ قرار الإدارة."
        )
    else:
        verification_lines = (
            f"**سجل التحقق (الوضع الحالي: {status_label}):**\n\n"
            f"• إيداع الضمان: —\n"
            f"• تحويل الليرة: —\n"
            f"• عنوان المستلم: `{buyer_wallet_escaped}`\n\n\n"
            f"الصفقة بحالة: {status_label}"
        )

    return (
        f"🛡️ **مراجعة وتدقيق صفقة ضمان #{deal.id}**\n\n\n"
        f"**تفاصيل الصفقة المالية:**\n\n"
        f"• وقت الإنشاء:  `{created_date_str}`\n"
        f"• الحالة الحالية:  `{status_label}`\n"
        f"• الكمية:  `{usdt_str}` USDT\n"
        f"• الإجمالي:  `{syp_str}` SYP\n"
        f"• سعر الصرف:  `{rate_str}` SYP\n"
        f"• عمولة المنصة:  `{fee_str}` USDT (البائع: `{seller_fee_str}` | المشتري: `{buyer_fee_str}`)\n"
        f"• مطلوب إيداع البائع:  `{seller_deposit_str}` USDT\n"
        f"• صافي استلام المشتري:  `{buyer_receives_str}` USDT\n"
        f"• محفظة الإيداع:  `{deal.deposit_address}`\n\n\n"
        f"🔴 **طرف البيع:**\n\n"
        f"• المعرف:  `{deal.seller_id}` ({seller_display})\n"
        f"• السجل:  `{seller_deals_count}` صفقة ({seller_rep})\n"
        f"• حساب استلام الليرة:  `{seller_account_escaped}`\n\n\n"
        f"🟢 **طرف الشراء:**\n\n"
        f"• المعرف:  `{deal.buyer_id}` ({buyer_display})\n"
        f"• السجل:  `{buyer_deals_count}` صفقة ({buyer_rep})\n"
        f"• محفظة استلام USDT:  `{buyer_wallet_escaped}`\n\n\n"
        f"{verification_lines}"
    )
