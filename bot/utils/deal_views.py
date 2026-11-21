from decimal import Decimal
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db.models import Deal, Offer, User, DealRating, PaymentProof, AuditLog, Negotiation
from bot.services.rating_service import format_stars
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
            return [[InlineKeyboardButton(text="🔙 الرئيسية", callback_data="menu_back_main")]]
        return []

    buttons = []
    if context == "browse":
        row = [InlineKeyboardButton(text=f"⚡ تنفيذ #{offer_id}", callback_data=f"start_deal_{offer_id}")]
        if negotiable and negotiable_type != "none":
            row.append(InlineKeyboardButton(text=f"🤝 تفاوض #{offer_id}", callback_data=f"start_neg_{offer_id}"))
        return [row]

    if context == "start":
        buttons.append([InlineKeyboardButton(text="⚡ تنفيذ العرض", callback_data=f"start_deal_{offer_id}")])
        if negotiable and negotiable_type != "none":
            buttons.append([InlineKeyboardButton(text="🤝 تفاوض", callback_data=f"start_neg_{offer_id}")])
        buttons.append([InlineKeyboardButton(text="🔙 الرئيسية", callback_data="menu_back_main")])
        return buttons

    # Default context == "offers"
    buttons.append([InlineKeyboardButton(text="⚡ تنفيذ العرض", callback_data=f"start_deal_{offer_id}")])
    if negotiable and negotiable_type != "none":
        buttons.append([InlineKeyboardButton(text="🤝 تفاوض", callback_data=f"start_neg_{offer_id}")])
    return buttons

STATUS_ARABIC_MAP = {
    "PENDING_OWNER_APPROVAL": "بانتظار موافقة صاحب العرض",
    "CREATED": "بانتظار بدء إيداع الضمان",
    "AWAITING_USDT_DEPOSIT": "بانتظار إيداع الضمان",
    "USDT_DEPOSITED": "تم إيداع الضمان بنجاح",
    "AWAITING_SYP_PAYMENT": "بانتظار تحويل الليرة",
    "SYP_PAYMENT_SENT": "تم إرسال الليرة وبانتظار تأكيد البائع",
    "SYP_PAYMENT_CONFIRMED": "تم تأكيد استلام الليرة",
    "ADMIN_APPROVAL": "مراجعة واعتماد الإدارة",
    "RELEASING": "جاري إطلاق الرصيد",
    "COMPLETED": "مكتملة بنجاح",
    "CANCELLED": "ملغاة",
    "EXPIRED": "منتهية الصلاحية",
    "DISPUTED": "مغلقة بعدم استلام (متابعة يدوية)",
}

async def format_admin_deal_review_text(deal: Deal, db_session: AsyncSession) -> str:
    """Format rich, detailed notification and inspection text for platform administrators."""
    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)
    created_date_str = format_local_datetime(deal.created_at) if deal.created_at else "غير محدد"
    offer_line = f"▫️ **رقم العرض المرتبط:** `#{deal.offer_id}`\n" if getattr(deal, "offer_id", None) else ""

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

    # Check if payment proof screenshot is attached
    proof_res = await db_session.execute(
        select(PaymentProof).where(PaymentProof.deal_id == deal.id).order_by(PaymentProof.id.desc()).limit(1)
    )
    proof_obj = proof_res.scalar_one_or_none()
    proof_status_line = "📸 إشعار الدفع: مرفق ✅ (متاح للعرض بالزر أدناه)" if proof_obj else "📸 إشعار الدفع: غير مرفق بعد"

    current_status = deal.status
    status_label = STATUS_ARABIC_MAP.get(current_status, current_status)

    deposit_tx = (getattr(deal, "deposit_tx_hash", None) or "").strip()
    payout_tx = (getattr(deal, "payout_tx_hash", None) or "").strip()
    dep_tx_line = f"\n▫️ **TxHash الإيداع:** `{escape_markdown(deposit_tx)}`" if deposit_tx else ""
    pay_tx_line = f"\n▫️ **TxHash الإطلاق:** `{escape_markdown(payout_tx)}`" if payout_tx else ""

    if current_status in ("ADMIN_APPROVAL", "SYP_PAYMENT_CONFIRMED"):
        verification_lines = (
            "📌 **سجل التحقق**\n"
            f"✅ إيداع الضمان: مؤكد ومحجوز{dep_tx_line}\n"
            "✅ تحويل الليرة: مؤكد استلامه من البائع\n"
            f"▫️ {proof_status_line}\n"
            f"▫️ محفظة المستلم: {'مسجلة ومتحقق منها ✅' if has_buyer_wallet else 'غير مسجلة بعد ⚠️'}\n\n"
            "الصفقة جاهزة للاعتماد وإطلاق الرصيد."
        )
    elif current_status == "COMPLETED":
        verification_lines = (
            "📌 **سجل التحقق**\n"
            f"✅ إيداع الضمان: مكتمل{dep_tx_line}\n"
            "✅ تحويل الليرة: مؤكد ومكتمل\n"
            f"▫️ {proof_status_line}\n"
            f"✅ محفظة المستلم: تم إطلاق الرصيد إليها{pay_tx_line}"
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
            f"▫️ {proof_status_line}\n"
            f"▫️ محفظة المستلم: {payout_status}"
        )
    elif current_status == "SYP_PAYMENT_SENT":
        payout_status = "مسجلة ✅" if has_buyer_wallet else "غير مسجلة بعد ⏳"
        verification_lines = (
            "📌 **سجل التحقق**\n"
            "✅ إيداع الضمان: مؤكد ومحجوز\n"
            "⏳ تحويل الليرة: بانتظار تأكيد البائع\n"
            f"▫️ {proof_status_line}\n"
            f"▫️ محفظة المستلم: {payout_status}"
        )
    elif current_status == "DISPUTED":
        verification_lines = (
            "📌 **سجل التحقق**\n"
            "🛡️ إيداع الضمان: محجوز لدى المنصة\n"
            "⚠️ تحويل الليرة: مغلقة بعدم استلام (متابعة يدوية)\n"
            f"▫️ {proof_status_line}\n\n"
            "الصفقة تتطلب مراجعة الإدارة والفصل اليدوي بين الطرفين."
        )
    else:
        verification_lines = (
            "📌 **سجل التحقق**\n"
            f"▫️ الوضع الحالي: {status_label}\n"
            f"▫️ {proof_status_line}"
        )

    ratings_res = await db_session.execute(
        select(DealRating).where(DealRating.deal_id == deal.id)
    )
    deal_ratings = ratings_res.scalars().all()
    ratings_section = ""
    if current_status == "COMPLETED" or deal_ratings:
        buyer_rating = next((r for r in deal_ratings if r.rater_id == deal.buyer_id), None)
        seller_rating = next((r for r in deal_ratings if r.rater_id == deal.seller_id), None)
        buyer_r_str = (
            f"{format_stars(buyer_rating.stars)} (`{buyer_rating.stars}/5`)"
            if buyer_rating else "لم يقيّم بعد"
        )
        seller_r_str = (
            f"{format_stars(seller_rating.stars)} (`{seller_rating.stars}/5`)"
            if seller_rating else "لم يقيّم بعد"
        )
        ratings_section = (
            "\n\n⭐ **تقييمات الصفقة**\n"
            f"▫️ **تقييم المشتري للبائع:** {buyer_r_str}\n"
            f"▫️ **تقييم البائع للمشتري:** {seller_r_str}"
        )

    return (
        f"🛡️ **مراجعة عملية الربط / الصفقة #{deal.id}**\n\n"
        "📋 **الملخص المالي**\n"
        f"▫️ **الحالة:** {status_label}\n"
        f"{offer_line}"
        f"▫️ **التاريخ:** `{created_date_str}`\n"
        f"▫️ **الكمية:** `{usdt_str} USDT`\n"
        f"▫️ **الإجمالي:** `{syp_str}` ل.س\n"
        f"▫️ **سعر الصرف:** `{rate_str}` ل.س\n"
        f"▫️ **إجمالي العمولة:** `{fee_str} USDT` (البائع `{seller_fee_str} USDT` | المشتري `{buyer_fee_str} USDT`)\n"
        f"▫️ **مطلوب إيداع البائع:** `{seller_deposit_str} USDT`\n"
        f"▫️ **صافي استلام المشتري:** `{buyer_receives_str} USDT`\n"
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
        f"{ratings_section}"
    )


async def can_admin_recover_cancelled_deal(deal: Deal, db_session: AsyncSession) -> tuple[bool, str]:
    """
    Validates whether a CANCELLED or EXPIRED deal is eligible for admin manual deposit recovery:
    1. Deal must have a non-empty deposit_address.
    2. Deal must not have been cancelled/rejected while still in PENDING_OWNER_APPROVAL (before deposit phase).
    3. Associated offer (if any) must not be 'completed' and must not have another open deal running on it.
    """
    if deal.status not in ("CANCELLED", "EXPIRED"):
        return False, f"حالة الصفقة الحالية ({deal.status}) لا تقبل الاستعادة."

    if not (deal.deposit_address or "").strip():
        return False, "لا يوجد عنوان محفظة إيداع مرتبط بهذه الصفقة."

    audit_res = await db_session.execute(
        select(AuditLog)
        .where(
            AuditLog.target_type == "deal",
            AuditLog.target_id == deal.id,
            AuditLog.action.in_(["CANCEL_DEAL", "REJECT_DEAL", "EXPIRE_DEAL", "ADMIN_FORCE_CANCEL_DEAL"])
        )
        .order_by(AuditLog.id.desc())
        .limit(1)
    )
    last_cancel_audit = audit_res.scalar_one_or_none()
    if last_cancel_audit and isinstance(last_cancel_audit.details, dict):
        prev_st = last_cancel_audit.details.get("previous_status")
        if prev_st == "PENDING_OWNER_APPROVAL":
            return (
                False,
                "⚠️ لا يمكن استعادة هذه الصفقة لأنها أُلغيت/رُفضت في مرحلة انتظار موافقة صاحب العرض قبل بدء مرحلة الإيداع."
            )

    if deal.offer_id:
        offer_res = await db_session.execute(select(Offer).where(Offer.id == deal.offer_id))
        offer = offer_res.scalar_one_or_none()
        if offer and offer.status == "completed":
            return False, "⚠️ لا يمكن استعادة الصفقة لأن العرض المرتبط بها قد اكتمل مسبقاً."
        if offer and offer.status == "negotiating":
            return False, "⚠️ لا يمكن استعادة الصفقة لأن العرض مرتبط حالياً بجلسة تفاوض جارية."

        conflict_res = await db_session.execute(
            select(Deal.id).where(
                Deal.offer_id == deal.offer_id,
                Deal.id != deal.id,
                Deal.status.not_in(["CANCELLED", "EXPIRED", "COMPLETED", "DISPUTED"])
            ).limit(1)
        )
        conflicting_deal_id = conflict_res.scalar_one_or_none()
        if conflicting_deal_id:
            return (
                False,
                f"⚠️ لا يمكن استعادة الصفقة لأن العرض مرتبط حالياً بصفقة أخرى جارية (#{conflicting_deal_id})."
            )

        neg_conflict_res = await db_session.execute(
            select(Negotiation.id).where(
                Negotiation.offer_id == deal.offer_id,
                Negotiation.status.in_(["PENDING_MAKER", "PENDING_TAKER"])
            ).limit(1)
        )
        conflicting_neg_id = neg_conflict_res.scalar_one_or_none()
        if conflicting_neg_id:
            return (
                False,
                f"⚠️ لا يمكن استعادة الصفقة لأن العرض مرتبط حالياً بجلسة تفاوض جارية (#{conflicting_neg_id})."
            )

    return True, ""


async def build_admin_deal_keyboard(deal: Deal, db_session: AsyncSession) -> InlineKeyboardMarkup:
    """
    Builds the comprehensive admin inspection keyboard:
    - Escrow release (ADMIN_APPROVAL)
    - Admin force confirm SYP (SYP_PAYMENT_SENT)
    - Admin close as dispute (post-deposit states: USDT_DEPOSITED, AWAITING_SYP_PAYMENT, SYP_PAYMENT_SENT, SYP_PAYMENT_CONFIRMED, ADMIN_APPROVAL)
    - Admin force cancel (early/open states)
    - View attached SYP payment proof screenshot (PaymentProof)
    - Open Seller / Buyer full user profile
    - Delete buyer/seller rating
    """
    buttons: list[list[InlineKeyboardButton]] = []

    if deal.status == "ADMIN_APPROVAL":
        buttons.append([InlineKeyboardButton(text="☑️ اعتماد وإطلاق الضمان", callback_data=f"ask_rel_{deal.id}")])
    elif deal.status in ("CREATED", "AWAITING_USDT_DEPOSIT") and (deal.deposit_address or "").strip():
        buttons.append([
            InlineKeyboardButton(text="✅ تأكيد إيداع الـ USDT يدوياً (طوارئ API)", callback_data=f"adm_ask_manual_dep_{deal.id}")
        ])
    elif deal.status in ("CANCELLED", "EXPIRED"):
        can_recover, _ = await can_admin_recover_cancelled_deal(deal, db_session)
        if can_recover:
            buttons.append([
                InlineKeyboardButton(text="🔄 استعادة الصفقة وتأكيد إيداع الـ USDT يدوياً", callback_data=f"adm_ask_manual_dep_{deal.id}")
            ])
    elif deal.status == "SYP_PAYMENT_SENT":
        buttons.append([
            InlineKeyboardButton(text="⚡ تأكيد استلام الليرة إدارياً", callback_data=f"adm_ask_force_conf_{deal.id}")
        ])

    if deal.status in (
        "USDT_DEPOSITED",
        "AWAITING_SYP_PAYMENT",
        "SYP_PAYMENT_SENT",
        "SYP_PAYMENT_CONFIRMED",
        "ADMIN_APPROVAL",
    ):
        buttons.append([
            InlineKeyboardButton(text="⚠️ إغلاق كنزاع (عدم استلام)", callback_data=f"adm_ask_force_dispute_{deal.id}")
        ])

    if deal.status in (
        "PENDING_OWNER_APPROVAL",
        "CREATED",
        "AWAITING_USDT_DEPOSIT",
    ):
        buttons.append([
            InlineKeyboardButton(text="❌ إلغاء الصفقة إدارياً", callback_data=f"adm_ask_force_cancel_{deal.id}")
        ])

    # Check if payment proof photo exists
    proof_res = await db_session.execute(
        select(PaymentProof).where(PaymentProof.deal_id == deal.id).order_by(PaymentProof.id.desc()).limit(1)
    )
    proof_obj = proof_res.scalar_one_or_none()
    if proof_obj and proof_obj.transaction_reference and proof_obj.transaction_reference.startswith("PHOTO_"):
        buttons.append([
            InlineKeyboardButton(text="📸 عرض صورة إشعار التحويل", callback_data=f"adm_view_proof_{deal.id}")
        ])

    # Direct links to Seller and Buyer profiles
    party_row = []
    if deal.seller_id:
        party_row.append(InlineKeyboardButton(text="🔴 ملف البائع", callback_data=f"adm_view_user_{deal.seller_id}"))
    if deal.buyer_id:
        party_row.append(InlineKeyboardButton(text="🟢 ملف المشتري", callback_data=f"adm_view_user_{deal.buyer_id}"))
    if party_row:
        buttons.append(party_row)

    # Ratings moderation buttons
    ratings_res = await db_session.execute(
        select(DealRating).where(DealRating.deal_id == deal.id)
    )
    deal_ratings = ratings_res.scalars().all()
    buyer_rating = next((r for r in deal_ratings if r.rater_id == deal.buyer_id), None)
    seller_rating = next((r for r in deal_ratings if r.rater_id == deal.seller_id), None)

    if buyer_rating:
        buttons.append([
            InlineKeyboardButton(
                text=f"🗑️ حذف تقييم المشتري ({buyer_rating.stars}★)",
                callback_data=f"adm_del_rating_{buyer_rating.id}"
            )
        ])
    if seller_rating:
        buttons.append([
            InlineKeyboardButton(
                text=f"🗑️ حذف تقييم البائع ({seller_rating.stars}★)",
                callback_data=f"adm_del_rating_{seller_rating.id}"
            )
        ])

    buttons.append([
        InlineKeyboardButton(text="🔄 تحديث البيانات", callback_data=f"adm_inspect_deal_{deal.id}"),
        InlineKeyboardButton(text="👑 لوحة الإدارة", callback_data="admin_main_menu"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


