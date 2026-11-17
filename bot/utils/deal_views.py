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
        rate = deal.amount_syp / deal.amount_usdt
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
        seller_method = str(deal.seller_payment_info.get("method") or "").strip()
        seller_account_info = str(deal.seller_payment_info.get("info") or "غير محدد").strip()
        if seller_method and seller_method not in ("غير محدد", "PENDING"):
            seller_payment_display = f"{escape_markdown(seller_method)}\n`{escape_markdown(seller_account_info)}`"
        else:
            seller_payment_display = f"\n`{escape_markdown(seller_account_info)}`"
    else:
        seller_account_info = str(deal.seller_payment_info or "غير محدد")
        seller_payment_display = f"\n`{escape_markdown(seller_account_info)}`"

    raw_buyer_wallet = (deal.buyer_payout_address or "").strip()
    has_buyer_wallet = bool(raw_buyer_wallet and raw_buyer_wallet not in ("PENDING", "غير محدد", "N/A"))
    buyer_wallet_line = (
        f"\n`{escape_markdown(raw_buyer_wallet)}`"
        if has_buyer_wallet
        else " غير مسجلة بعد"
    )

    current_status = deal.status
    status_label = STATUS_ARABIC_MAP.get(current_status, current_status)

    if current_status in ("ADMIN_APPROVAL", "SYP_PAYMENT_CONFIRMED"):
        verification_lines = (
            "📌 **سجل التحقق**\n"
            "⏳ إيداع الضمان: مؤكد من البائع (وضع المحاكاة)\n"
            "✅ تحويل الليرة: مؤكد استلامه من البائع\n"
            "✅ محفظة المستلم: مسجلة ومتحقق منها\n\n"
            "الصفقة جاهزة للاعتماد وإطلاق الرصيد."
        )
    elif current_status == "COMPLETED":
        verification_lines = (
            "📌 **سجل التحقق**\n"
            "✅ إيداع الضمان: مكتمل (وضع المحاكاة)\n"
            "✅ تحويل الليرة: مؤكد ومكتمل\n"
            "✅ محفظة المستلم: تم إطلاق الرصيد إليها"
        )
    elif current_status in ("PENDING_OWNER_APPROVAL", "CREATED", "AWAITING_USDT_DEPOSIT"):
        payout_status = "مسجلة ✅" if has_buyer_wallet else "غير مسجلة بعد ⏳"
        verification_lines = (
            "📌 **سجل التحقق**\n"
            "⏳ إيداع الضمان: بانتظار البائع\n"
            "⏳ تحويل الليرة: لم يبدأ بعد\n"
            f"▫️ محفظة المستلم: {payout_status}"
        )
    elif current_status in ("USDT_DEPOSITED", "AWAITING_SYP_PAYMENT"):
        payout_status = "مسجلة ✅" if has_buyer_wallet else "غير مسجلة بعد ⏳"
        verification_lines = (
            "📌 **سجل التحقق**\n"
            "✅ إيداع الضمان: مؤكد ومحجوز\n"
            "⏳ تحويل الليرة: بانتظار المشتري\n"
            f"▫️ محفظة المستلم: {payout_status}"
        )
    elif current_status == "SYP_PAYMENT_SENT":
        payout_status = "مسجلة ✅" if has_buyer_wallet else "غير مسجلة بعد ⏳"
        verification_lines = (
            "📌 **سجل التحقق**\n"
            "✅ إيداع الضمان: مؤكد ومحجوز\n"
            "⏳ تحويل الليرة: بانتظار تأكيد البائع\n"
            f"▫️ محفظة المستلم: {payout_status}"
        )
    elif current_status == "DISPUTED":
        verification_lines = (
            "📌 **سجل التحقق**\n"
            "🛡️ إيداع الضمان: محجوز لدى المنصة\n"
            "⚠️ تحويل الليرة: نزاع مفتوح بين الطرفين\n\n"
            "الصفقة تتطلب مراجعة الإدارة وحسم النزاع."
        )
    else:
        verification_lines = (
            "📌 **سجل التحقق**\n"
            f"▫️ الوضع الحالي: {status_label}"
        )

    return (
        f"🛡️ **مراجعة الصفقة #{deal.id}**\n\n"
        "📋 **الملخص المالي**\n"
        f"▫️ **الحالة:** {status_label}\n"
        f"▫️ **التاريخ:** `{created_date_str}`\n"
        f"▫️ **الكمية:** `{usdt_str}` دولار رقمي\n"
        f"▫️ **الإجمالي:** `{syp_str}` ل.س\n"
        f"▫️ **سعر الصرف:** `{rate_str}` ل.س\n"
        f"▫️ **إجمالي العمولة:** `{fee_str}` (البائع `{seller_fee_str}` | المشتري `{buyer_fee_str}`)\n"
        f"▫️ **مطلوب إيداع البائع:** `{seller_deposit_str}` دولار رقمي\n"
        f"▫️ **صافي استلام المشتري:** `{buyer_receives_str}` دولار رقمي\n"
        f"▫️ **محفظة الإيداع:**\n`{deal.deposit_address}`\n\n"
        "🔴 **طرف البيع**\n"
        f"▫️ **الحساب:** `{deal.seller_id}` ({seller_display})\n"
        f"▫️ **السجل:** `{seller_deals_count}` صفقة ({seller_rep})\n"
        f"▫️ **حساب الاستلام:** {seller_payment_display}\n\n"
        "🟢 **طرف الشراء**\n"
        f"▫️ **الحساب:** `{deal.buyer_id}` ({buyer_display})\n"
        f"▫️ **السجل:** `{buyer_deals_count}` صفقة ({buyer_rep})\n"
        f"▫️ **محفظة الاستلام:**{buyer_wallet_line}\n\n"
        f"{verification_lines}"
    )
