from decimal import Decimal
from datetime import datetime, timezone, timedelta
import logging

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from db.models import Deal, Offer, User, PaymentProof, Dispute, DealRating
from bot.states.flow_states import DealPaymentFSM
from bot.services.audit_service import add_audit
from bot.services.channel import update_channel_offer_message
from bot.services.rating_service import (
    build_deal_rating_prompt,
    recalculate_user_reputation,
    format_stars,
    STAR_LABELS,
)
from config.settings import settings
from bot.utils.deal_views import format_admin_deal_review_text
from bot.utils.deal_card import push_deal_card, render_deal_card
from bot.utils.deal_guard import acquire_deal_lock
from bot.utils.validators import validate_bep20_address
from bot.utils.branding import BOT_NAME
from bot.utils.formatters import escape_markdown, format_syp, format_usdt
from bot.utils.transitions import can_seller_confirm_syp, can_admin_release_escrow
from bot.handlers.deals.payment_info import (
    apply_buyer_payout_wallet,
    forward_proof_photo_to_seller,
    prompt_seller_payment_info_flow,
    start_set_seller_info,
)

logger = logging.getLogger(__name__)

settlement_router = Router(name="deals_settlement")


# ================= USDT ESCROW DEPOSIT HANDLERS =================

@settlement_router.callback_query(F.data.startswith("man_aw_dep_"))
async def manual_await_usdt_deposit(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses="CREATED",
        allowed_roles="seller",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    now = datetime.now(timezone.utc)
    deal.status = "AWAITING_USDT_DEPOSIT"
    deal.expires_at = now + timedelta(minutes=15)
    await db_session.commit()

    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)
    seller_fee = getattr(deal, "seller_fee_usdt", None) or Decimal("0")
    total_deposit_usdt = deal.amount_usdt + seller_fee
    total_deposit_str = format_usdt(total_deposit_usdt)

    fee_note = f"\n▫️ **عمولة البائع المشمولة:** `{format_usdt(seller_fee)} USDT`" if seller_fee > 0 else ""

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأكيد إيداع رصيد الضمان", callback_data=f"man_dep_{deal.id}")]
    ])
    escaped_deposit = escape_markdown(deal.deposit_address)
    await callback.message.edit_text(
        f"🔐 **صفقة #{deal.id} — مطلوب إيداع الضمان**\n\n"
        f"▫️ **المبلغ المطلوب تحويله:** `{total_deposit_str} USDT`{fee_note}\n"
        f"▫️ **المقابل بالليرة السورية:** `{syp_str}` ل.س\n"
        f"▫️ **المهلة المتاحة:** 15 دقيقة\n"
        f"▫️ **الشبكة:** `BEP-20`\n\n"
        f"📥 **محفظة الإيداع:**\n"
        f"`{escaped_deposit}`\n\n"
        f"⚠️ يرجى التحويل بدقة على العنوان أعلاه، ثم الضغط على زر تأكيد الإيداع.",
        reply_markup=kb,
        parse_mode="Markdown"
    )

    # Notify Buyer
    try:
        await callback.bot.send_message(
            chat_id=deal.buyer_id,
            text=(
                f"ℹ️ **تحديث صفقة #{deal.id}**\n\n"
                f"▫️ **دورك:** 🟢 مشتري\n"
                f"▫️ **الكمية:** `{usdt_str} USDT`\n"
                f"▫️ **المقابل:** `{syp_str}` ل.س\n\n"
                f"بدأ البائع مهلة إيداع رصيد الضمان (15 دقيقة)، وسيتم إشعارك فور تأكيد الإيداع."
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify buyer {deal.buyer_id}: {e}")


@settlement_router.callback_query(F.data.startswith("man_dep_"))
async def manual_usdt_deposited(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses="AWAITING_USDT_DEPOSIT",
        allowed_roles="seller",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    deal.status = "USDT_DEPOSITED"

    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)

    # Check if seller payment info is ALREADY populated (e.g. from Sell Offer creation or Buy Offer taker input)
    seller_info_val = ""
    if isinstance(deal.seller_payment_info, dict):
        seller_info_val = str(deal.seller_payment_info.get("info") or "").strip()
    elif isinstance(deal.seller_payment_info, str):
        seller_info_val = deal.seller_payment_info.strip()

    if seller_info_val and seller_info_val not in ["PENDING", "غير محدد", "N/A"]:
        # Seller payment details already attached! Auto-advance directly to AWAITING_SYP_PAYMENT seamlessly
        deal.status = "AWAITING_SYP_PAYMENT"
        deal.expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        await db_session.commit()
        await callback.answer("تم تأكيد إيداع الضمان واعتماد بيانات الحساب تلقائياً ✅")
        await push_deal_card(deal.id, callback.bot)
        return

    await db_session.commit()

    # Notify Buyer only when seller still needs to enter payment details
    try:
        await callback.bot.send_message(
            chat_id=deal.buyer_id,
            text=(
                f"🔒 **تحديث صفقة #{deal.id} — تم حجز رصيد الضمان**\n\n"
                f"▫️ **دورك:** 🟢 مشتري\n"
                f"▫️ **رصيد الضمان المحتجز:** `{usdt_str} USDT`\n"
                f"▫️ **المبلغ المقابل:** `{syp_str}` ل.س\n\n"
                f"تم تأكيد حجز الضمان في محفظة الوساطة لـ {BOT_NAME}.\n"
                f"يقوم البائع الآن بتحديد حساب الاستلام لتتمكن من التحويل."
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify buyer {deal.buyer_id}: {e}")

    header = (
        f"🔒 **صفقة #{deal.id} — تم حجز الضمان بنجاح**\n\n"
        f"▫️ **الكمية المحتجزة:** `{usdt_str} USDT`\n"
        f"▫️ **المبلغ المقابل:** `{syp_str}` ل.س\n\n"
    )
    await prompt_seller_payment_info_flow(
        deal=deal,
        callback=callback,
        state=state,
        db_session=db_session,
        header_prefix=header,
        commit_before_send=False,
    )


# ================= SYP PAYMENT & PROOF HANDLERS =================

@settlement_router.callback_query(F.data.startswith("man_aw_syp_"))
async def manual_await_syp(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses=["USDT_DEPOSITED", "AWAITING_SYP_PAYMENT"],
        allowed_roles="seller",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    seller_method = ""
    seller_info_val = ""
    if isinstance(deal.seller_payment_info, dict):
        seller_method = str(deal.seller_payment_info.get("method") or "")
        seller_info_val = str(deal.seller_payment_info.get("info") or "")
    elif isinstance(deal.seller_payment_info, str):
        seller_info_val = deal.seller_payment_info

    if not seller_info_val or seller_info_val in ["PENDING", "غير محدد", "N/A"]:
        # Seamlessly route seller to set payment info to eliminate dead-ends
        await start_set_seller_info(callback, state, db_session)
        return

    prev_status = deal.status
    deal.status = "AWAITING_SYP_PAYMENT"
    if prev_status != "AWAITING_SYP_PAYMENT" or getattr(deal, "expires_at", None) is None:
        deal.expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    await db_session.commit()

    if seller_method and seller_method not in ["غير محدد", "PENDING"]:
        payment_info_str = f"{seller_method}\n`{escape_markdown(seller_info_val)}`"
    else:
        payment_info_str = f"`{escape_markdown(seller_info_val)}`"
    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)

    # 1. Update Seller's UI
    await callback.message.edit_text(
        f"⏳ **صفقة #{deal.id} — بانتظار تحويل الليرة السورية**\n\n"
        f"▫️ **الكمية:** `{usdt_str} USDT`\n"
        f"▫️ **المبلغ المقابل:** `{syp_str}` ل.س\n"
        f"▫️ **حساب الاستلام المعتمد:**\n{payment_info_str}\n\n"
        f"تم إشعار المشتري بالتحويل، وسيصلك إشعار فور تأكيد الدفع من قِبله.",
        parse_mode="Markdown"
    )

    # 2. Send prompt with Action button directly to the BUYER
    buyer_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 تأكيد تحويل المبلغ وإرفاق الإشعار", callback_data=f"man_syp_{deal.id}")]
    ])
    try:
        await callback.bot.send_message(
            chat_id=deal.buyer_id,
            text=(
                f"💸 **صفقة #{deal.id} — مطلوب تحويل الليرة السورية**\n\n"
                f"▫️ **المبلغ المطلوب تحويله:** `{syp_str}` ل.س\n"
                f"▫️ **رصيد الضمان المحجوز:** `{usdt_str} USDT`\n"
                f"▫️ **حساب استلام البائع:**\n{payment_info_str}\n\n"
                f"⚠️ يرجى التحويل بدقة على الحساب أعلاه، ثم الضغط على زر التأكيد وإرفاق إشعار الدفع."
            ),
            reply_markup=buyer_kb,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify buyer {deal.buyer_id}: {e}")


@settlement_router.callback_query(F.data.startswith("man_syp_"))
async def manual_syp_sent(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses="AWAITING_SYP_PAYMENT",
        allowed_roles="buyer",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    await state.set_state(DealPaymentFSM.waiting_for_syp_proof_photo)
    await state.update_data(deal_id=deal.id)

    syp_str = format_syp(deal.amount_syp)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])

    await callback.message.edit_text(
        f"📸 **إرفاق إشعار التحويل — صفقة #{deal.id}**\n"
        f"〰️〰️〰️〰️〰️〰️〰️\n"
        f"💰 يجب إرسال المبلغ : `{syp_str}` ل.س\n"
        f"📸 يرجى إرسال لقطة شاشة تثبت عملية التحويل:",
        reply_markup=cancel_kb,
        parse_mode="Markdown"
    )
    await callback.answer()


@settlement_router.message(DealPaymentFSM.waiting_for_syp_proof_photo, F.photo)
async def process_syp_proof_photo(message: Message, state: FSMContext, db_session: AsyncSession):
    data = await state.get_data()
    deal_id = data.get('deal_id')
    if not deal_id:
        await message.answer("حدث خطأ في استعادة بيانات الجلسة.")
        await state.clear()
        return

    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=message.from_user.id,
        expected_statuses="AWAITING_SYP_PAYMENT",
        allowed_roles="buyer",
        db_session=db_session
    )
    if not valid:
        await db_session.commit()
        await message.answer(err_msg)
        await state.clear()
        return

    photo = message.photo[-1]
    photo_file_id = photo.file_id
    ref = f"PHOTO_{photo_file_id}"

    # Check for existing proof to avoid UniqueViolation/MissingGreenlet
    existing_proof_res = await db_session.execute(
        select(PaymentProof).where(PaymentProof.transaction_reference == ref)
    )
    if existing_proof_res.scalar_one_or_none():
        await message.answer(
            "⚠️ **هذه الصورة استُخدمت سابقاً!**\n\n"
            "يرجى إرسال لقطة شاشة جديدة لإشعار التحويل الخاص بهذه العملية.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
            ]),
            parse_mode="Markdown"
        )
        return

    try:
        proof = PaymentProof(
            deal_id=deal.id,
            transaction_reference=ref,
            amount=deal.amount_syp,
            submitted_by=message.from_user.id
        )
        db_session.add(proof)
        await db_session.commit()
    except Exception as e:
        logger.warning(f"PaymentProof creation log: {e}")

    existing_buyer_addr = (deal.buyer_payout_address or "").strip()
    is_existing_valid, normalized_existing, _ = validate_bep20_address(existing_buyer_addr) if (existing_buyer_addr and existing_buyer_addr not in ["PENDING", "غير محدد", "N/A"]) else (False, "", "")

    await state.clear()

    if is_existing_valid:
        await apply_buyer_payout_wallet(
            deal=deal,
            wallet_address=normalized_existing,
            bot=message.bot,
            db_session=db_session,
            event=message,
            photo_file_id=photo_file_id
        )
    else:
        deal.status = "SYP_PAYMENT_SENT"
        deal.expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
        await db_session.commit()

        # Push deal card ONLY to buyer:
        await push_deal_card(deal.id, message.bot, specific_user_id=deal.buyer_id)
        await forward_proof_photo_to_seller(deal, photo_file_id, message.bot, db_session)

        user_res = await db_session.execute(select(User).where(User.telegram_id == deal.buyer_id))
        buyer_user = user_res.scalar_one_or_none()
        saved_wallet = buyer_user.saved_usdt_address.strip() if (buyer_user and buyer_user.saved_usdt_address) else None
        is_saved_valid, normalized_saved, _ = validate_bep20_address(saved_wallet) if saved_wallet else (False, "", "")

        if is_saved_valid:
            gate_text = (
                f"✅ تم حفظ إشعار التحويل\n\n"
                f"📝 **تأكيد معلومات USDT (BEP-20) — صفقة #{deal.id}:**\n"
                f"`{escape_markdown(normalized_saved)}`"
            )
            gate_kb = InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ اعتماد", callback_data=f"use_saved_buyer_wallet_{deal.id}"),
                    InlineKeyboardButton(text="✏️ تعديل", callback_data=f"manual_set_buyer_wallet_{deal.id}")
                ],
                [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
            ])
            await message.answer(gate_text, reply_markup=gate_kb, parse_mode="Markdown")
        else:
            await state.set_state(DealPaymentFSM.waiting_for_buyer_wallet)
            await state.update_data(deal_id=deal.id)
            await message.answer(
                f"✅ تم حفظ إشعار التحويل\n\n"
                f"💵 أرسل عنوان USDT (BEP-20) فقط، من دون أي نص أو كلمات إضافية.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
                ]),
                parse_mode="Markdown"
            )


@settlement_router.message(DealPaymentFSM.waiting_for_syp_proof_photo)
async def process_syp_proof_photo_invalid(message: Message):
    await message.answer(
        "⚠️ **يرجى إرسال لقطة شاشة (صورة إشعار الدفع) حصراً!**\n\n"
        "قم بإرفاق صورة الإشعار للمتابعة وإرسالها للبائع:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
        ]),
        parse_mode="Markdown"
    )


# ================= SELLER SYP CONFIRMATION & DISPUTE =================

@settlement_router.callback_query(F.data.startswith("ask_conf_"))
async def ask_confirm_syp(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()

    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return

    allowed, err_msg = can_seller_confirm_syp(deal.status, callback.from_user.id, deal.seller_id)
    if not allowed:
        await callback.answer(err_msg, show_alert=True)
        return

    await db_session.commit()

    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)

    text = (
        f"⚠️ **تأكيد استلام المبلغ — صفقة #{deal.id}**\n"
        f"〰️〰️〰️〰️〰️〰️〰️\n"
        f"💰 المبلغ المطلوب استلامه : `{syp_str}` ل.س\n"
        f"💵 رصيد الضمان المقابل : `{usdt_str} USDT`\n"
        f"〰️〰️〰️〰️〰️〰️〰️\n"
        f"⚠️ تأكد من وصول كامل المبلغ إلى حسابك قبل التأكيد (لا يمكن التراجع)."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ نعم، أؤكد الاستلام", callback_data=f"man_conf_{deal.id}")],
        [InlineKeyboardButton(text="↩️ رجوع", callback_data=f"back_seller_deal_{deal.id}")]
    ])
    try:
        if callback.message.photo:
            await callback.message.edit_caption(caption=text, reply_markup=kb, parse_mode="Markdown")
        else:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@settlement_router.callback_query(F.data.startswith("back_seller_deal_"))
async def cq_back_seller_deal(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()
    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return
    await db_session.commit()

    if deal.status == "SYP_PAYMENT_SENT" and callback.message.photo:
        syp_str = format_syp(deal.amount_syp)
        usdt_str = format_usdt(deal.amount_usdt)
        seller_info_val = ""
        seller_method = ""
        if isinstance(deal.seller_payment_info, dict):
            seller_method = str(deal.seller_payment_info.get("method") or "")
            seller_info_val = str(deal.seller_payment_info.get("info") or "")
        elif isinstance(deal.seller_payment_info, str):
            seller_info_val = deal.seller_payment_info
        payment_info_str = (
            f"{seller_method}\n`{escape_markdown(seller_info_val)}`"
            if (seller_method and seller_method not in ["غير محدد", "PENDING"])
            else f"`{escape_markdown(seller_info_val)}`"
        )

        seller_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ تأكيد الاستلام", callback_data=f"ask_conf_{deal.id}"),
                InlineKeyboardButton(text="⚠️ لم أستلم", callback_data=f"dispute_deal_{deal.id}")
            ]
        ])
        caption = (
            f"🧾 **إشعار تحويل — صفقة #{deal.id}**\n"
            f"〰️〰️〰️〰️〰️〰️〰️\n"
            f"💰 المبلغ المحول : `{syp_str}` ل.س\n"
            f"💵 الضمان المقابل : `{usdt_str} USDT`\n"
            f"👤 حساب الاستلام :\n{payment_info_str}\n"
            f"〰️〰️〰️〰️〰️〰️〰️\n"
            f"⚠️ تأكد من وصول المبلغ إلى حسابك الفعلي قبل التأكيد."
        )
        try:
            await callback.message.edit_caption(caption=caption, reply_markup=seller_kb, parse_mode="Markdown")
        except Exception:
            pass
    else:
        card_text, kb = render_deal_card(deal, callback.from_user.id)
        try:
            await callback.message.edit_text(card_text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await callback.message.answer(card_text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


async def _dispatch_admin_approval(
    deal: Deal,
    bot: Bot,
    db_session: AsyncSession,
    origin_callback: CallbackQuery | None = None
) -> None:
    """Dispatches deal to admin review and notifies counterparties safely post-commit."""
    admin_alert_text = await format_admin_deal_review_text(deal, db_session)
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="☑️ اعتماد وإطلاق الضمان", callback_data=f"ask_rel_{deal.id}")],
        [InlineKeyboardButton(text="🔍 فحص تفصيلي للصفقة", callback_data=f"adm_inspect_deal_{deal.id}")]
    ])

    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)

    # 1. Update origin message if present
    if origin_callback:
        try:
            is_photo_msg = bool(getattr(origin_callback.message, "photo", None))
            if origin_callback.from_user.id in settings.ADMIN_IDS:
                if is_photo_msg:
                    await origin_callback.message.edit_caption(
                        caption=admin_alert_text,
                        reply_markup=admin_kb,
                        parse_mode="Markdown"
                    )
                else:
                    await origin_callback.message.edit_text(
                        admin_alert_text,
                        reply_markup=admin_kb,
                        parse_mode="Markdown"
                    )
            elif origin_callback.from_user.id == deal.seller_id:
                seller_text = (
                    f"✅ تم تأكيد استلام المبلغ لعملية الربط رقم #{deal.id}.\n"
                    f"بانتظار إرسال الـ USDT من الإدارة."
                )
                if is_photo_msg:
                    await origin_callback.message.edit_caption(
                        caption=seller_text,
                        reply_markup=None,
                        parse_mode="Markdown"
                    )
                else:
                    await origin_callback.message.edit_text(
                        seller_text,
                        reply_markup=None,
                        parse_mode="Markdown"
                    )
            else:
                fallback_text = (
                    f"✅ تم تأكيد استلام المبلغ لعملية الربط رقم #{deal.id}.\n"
                    f"بانتظار إرسال الـ USDT من الإدارة."
                )
                if is_photo_msg:
                    await origin_callback.message.edit_caption(
                        caption=fallback_text,
                        reply_markup=None,
                        parse_mode="Markdown"
                    )
                else:
                    await origin_callback.message.edit_text(
                        fallback_text,
                        parse_mode="Markdown"
                    )
        except Exception as e:
            logger.warning(f"Failed to edit origin message in admin dispatch for deal #{deal.id}: {e}")

    # Synchronize Deal Cards for counterparties
    try:
        await push_deal_card(deal.id, bot, notify_handoff=False)
    except Exception as e:
        logger.warning(f"Failed to push deal card in _dispatch_admin_approval for deal #{deal.id}: {e}")

    # 2. Notify Buyer (single notification)
    if not (origin_callback and origin_callback.from_user.id == deal.buyer_id):
        try:
            await bot.send_message(
                chat_id=deal.buyer_id,
                text=(
                    f"✅ تم تأكيد استلام المبلغ لعملية الربط رقم #{deal.id}.\n"
                    f"بانتظار إرسال الـ USDT من الإدارة."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Failed to notify buyer {deal.buyer_id}: {e}")

    # 3. If buyer wallet is pending, prompt buyer
    if not deal.buyer_payout_address or deal.buyer_payout_address in ["PENDING", "غير محدد", "N/A"]:
        buyer_wallet_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💵 إدخال عنوان USDT", callback_data=f"set_buyer_payout_{deal.id}")]
        ])
        try:
            await bot.send_message(
                chat_id=deal.buyer_id,
                text=(
                    f"⚠️ **صفقة #{deal.id}** — يرجى إدخال عنوان USDT (BEP-20) لاستلام الرصيد:"
                ),
                reply_markup=buyer_wallet_kb,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Failed to prompt buyer for payout wallet: {e}")

    # 4. Broadcast to admins
    for admin_id in settings.ADMIN_IDS:
        if origin_callback and origin_callback.from_user.id == admin_id:
            continue
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=admin_alert_text,
                reply_markup=admin_kb,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Failed to send admin notification to admin {admin_id} for deal #{deal.id}: {e}")


@settlement_router.callback_query(F.data.startswith("man_conf_"))
async def manual_syp_confirmed(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses="SYP_PAYMENT_SENT",
        allowed_roles="seller",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    # Atomic 1-transaction 1-commit: SYP_PAYMENT_SENT -> SYP_PAYMENT_CONFIRMED -> ADMIN_APPROVAL
    deal.status = "ADMIN_APPROVAL"
    deal.expires_at = None
    if getattr(callback.message, "photo", None):
        # Detach seller_card_msg_id from the proof photo so push_deal_card sends a new text card below it without deleting the photo
        deal.seller_card_msg_id = None
    await db_session.commit()

    await _dispatch_admin_approval(deal, callback.bot, db_session, origin_callback=callback)
    await callback.answer()


@settlement_router.callback_query(F.data.startswith("dispute_deal_"))
async def handle_non_receipt_dispute(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses=["AWAITING_SYP_PAYMENT", "SYP_PAYMENT_SENT"],
        allowed_roles="seller",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    now = datetime.now(timezone.utc)
    deal.status = "DISPUTED"
    deal.expires_at = None
    if getattr(callback.message, "photo", None):
        # Detach seller_card_msg_id from the proof photo so push_deal_card keeps the photo in chat history
        deal.seller_card_msg_id = None

    target_offer = None
    offer_id_val = getattr(deal, "offer_id", None)
    if offer_id_val and hasattr(db_session, "execute"):
        try:
            offer_res = await db_session.execute(
                select(Offer).where(Offer.id == offer_id_val).with_for_update()
            )
            offer = offer_res.scalar_one_or_none()
            if offer and offer.status == "reserved":
                offer.status = "cancelled"
                offer.status_changed_at = now
                target_offer = offer
        except Exception as e:
            logger.warning(f"Failed to close associated offer #{offer_id_val} on dispute for deal #{deal.id}: {e}")

    if hasattr(db_session, "add"):
        try:
            new_dispute = Dispute(
                deal_id=deal.id,
                opened_by=callback.from_user.id,
                reason="إبلاغ البائع بعدم استلام الدفعة بالليرة السورية (متابعة يدوية)"
            )
            db_session.add(new_dispute)
        except Exception as e:
            logger.warning(f"Failed to add Dispute row for deal #{deal.id}: {e}")

    add_audit(
        session=db_session,
        action="REPORT_NON_RECEIPT",
        target_type="deal",
        target_id=deal.id,
        actor_id=callback.from_user.id,
        actor_type="USER",
        details={"reported_by": callback.from_user.id}
    )
    await db_session.commit()

    if target_offer:
        try:
            await update_channel_offer_message(callback.bot, target_offer)
        except Exception as e:
            logger.warning(f"Failed to update channel offer message on dispute for deal #{deal.id}: {e}")

    support_username = (settings.SUPPORT_USERNAME or "AlphaSupport").lstrip('@')
    support_link = f"https://t.me/{support_username}"
    escaped_support = escape_markdown(support_username)

    notice_text = (
        f"⚠️ **صفقة #{deal.id} — إغلاق آلي وتحويل للمتابعة اليدوية**\n"
        f"〰️〰️〰️〰️〰️〰️〰️\n"
        f"أبلغ البائع بعدم استلام الدفعة بالليرة السورية.\n"
        f"تم إغلاق الصفقة آلياً على البوت وتحويل الملف إلى الإدارة للمتابعة والفصل اليدوي بين الطرفين.\n\n"
        f"👨‍💻 **يرجى التواصل المباشر مع الدعم الفني:** @{escaped_support}"
    )

    support_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👨‍💻 التواصل مع الدعم الفني", url=support_link)]
    ])

    try:
        if getattr(callback.message, "photo", None):
            await callback.message.edit_caption(caption=notice_text, reply_markup=support_kb, parse_mode="Markdown")
        else:
            await callback.message.edit_text(notice_text, reply_markup=support_kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(notice_text, reply_markup=support_kb, parse_mode="Markdown")

    try:
        await callback.bot.send_message(
            chat_id=deal.buyer_id,
            text=notice_text,
            reply_markup=support_kb,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to send non-receipt notice to buyer {deal.buyer_id}: {e}")

    admin_notice = (
        f"🚨 **إبلاغ عدم استلام (متابعة يدوية) — صفقة #{deal.id}**\n"
        f"〰️〰️〰️〰️〰️〰️〰️\n"
        f"▫️ **البائع:** `{deal.seller_id}`\n"
        f"▫️ **المشتري:** `{deal.buyer_id}`\n"
        f"▫️ **الكمية:** `{format_usdt(deal.amount_usdt)} USDT`\n"
        f"▫️ **المبلغ المقابل:** `{format_syp(deal.amount_syp)}` ل.س\n\n"
        f"أبلغ البائع عن عدم استلام المبلغ بالليرة السورية. تم إغلاق الصفقة آلياً على البوت لتتم متابعتها وتسويتها يدوياً بين الطرفين والإدارة."
    )
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 فحص تفصيلي للصفقة", callback_data=f"adm_inspect_deal_{deal.id}")]
    ])
    for admin_id in settings.ADMIN_IDS:
        try:
            await callback.bot.send_message(chat_id=admin_id, text=admin_notice, reply_markup=admin_kb, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Failed to notify admin {admin_id} for dispute on deal #{deal.id}: {e}")

    try:
        await push_deal_card(deal.id, callback.bot, notify_handoff=False)
    except Exception as e:
        logger.warning(f"Failed to push deal card on dispute for deal #{deal.id}: {e}")

    await callback.answer("تم إغلاق الصفقة آلياً وتحويل الملف للمتابعة اليدوية مع الدعم الفني.")


# ================= ADMIN REVIEW & ESCROW RELEASE =================

@settlement_router.callback_query(F.data.startswith("man_adm_appr_"))
async def manual_admin_approval(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(
        select(Deal).where(Deal.id == deal_id).with_for_update()
    )
    deal = result.scalar_one_or_none()

    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return

    if callback.from_user.id not in (deal.seller_id, deal.buyer_id) and callback.from_user.id not in settings.ADMIN_IDS:
        await callback.answer("غير مصرح: هذا الإجراء ليس من صلاحيتك بهذه الصفقة.", show_alert=True)
        return

    if deal.status != "SYP_PAYMENT_CONFIRMED":
        if deal.status == "ADMIN_APPROVAL":
            await callback.answer("الصفقة قيد المراجعة والاعتماد الإداري بالفعل.", show_alert=True)
            return
        await callback.answer("لا يمكن تنفيذ هذا الإجراء في هذه المرحلة من الصفقة.", show_alert=True)
        return

    deal.status = "ADMIN_APPROVAL"
    await db_session.commit()

    await _dispatch_admin_approval(deal, callback.bot, db_session, origin_callback=callback)
    await callback.answer()


@settlement_router.callback_query(F.data.startswith("ask_rel_"))
async def ask_admin_release(callback: CallbackQuery, db_session: AsyncSession):
    if callback.from_user.id not in settings.ADMIN_IDS:
        await callback.answer("غير مصرح: هذه الصلاحية للإدارة فقط.", show_alert=True)
        return

    deal_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()

    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return

    allowed, err_msg = can_admin_release_escrow(deal.status, callback.from_user.id, settings.ADMIN_IDS)
    if not allowed:
        await callback.answer(err_msg, show_alert=True)
        return

    buyer_addr = (deal.buyer_payout_address or "").strip()
    if not buyer_addr or buyer_addr in ["PENDING", "غير محدد", "N/A"]:
        await callback.answer("⚠️ لا يمكن الإطلاق: عنوان محفظة المشتري غير مسجل بعد.", show_alert=True)
        return

    seller_info_val = ""
    if isinstance(deal.seller_payment_info, dict):
        seller_info_val = str(deal.seller_payment_info.get("info") or "").strip()
    elif isinstance(deal.seller_payment_info, str):
        seller_info_val = deal.seller_payment_info.strip()

    if not seller_info_val or seller_info_val in ["PENDING", "غير محدد", "N/A"]:
        await callback.answer("⚠️ لا يمكن الإطلاق: بيانات حساب استلام البائع غير مسجلة بعد.", show_alert=True)
        return

    await db_session.commit()

    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)
    buyer_wallet_escaped = escape_markdown(buyer_addr)

    text = (
        f"⚠️ **تأكيد اعتماد وإطلاق الضمان — صفقة #{deal.id}**\n\n"
        f"▫️ **الكمية:** `{usdt_str} USDT`\n"
        f"▫️ **المبلغ المقابل:** `{syp_str}` ل.س\n"
        f"▫️ **الشبكة:** `BEP-20`\n"
        f"📥 **محفظة استلام المشتري:**\n"
        f"`{buyer_wallet_escaped}`\n\n"
        f"سيتم إطلاق رصيد الضمان فوراً إلى محفظة المشتري وإتمام الصفقة.\n"
        f"⚠️ **تنبيه:** هذا الإجراء نهائي ولا يمكن التراجع عنه. هل ترغب بالتأكيد؟"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ نعم، تأكيد وإطلاق الضمان", callback_data=f"man_rel_{deal.id}")],
        [InlineKeyboardButton(text="↩️ رجوع", callback_data=f"back_admin_deal_{deal.id}")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@settlement_router.callback_query(F.data.startswith("back_admin_deal_"))
async def cq_back_admin_deal(callback: CallbackQuery, db_session: AsyncSession):
    if callback.from_user.id not in settings.ADMIN_IDS:
        await callback.answer("غير مصرح لك.", show_alert=True)
        return
    deal_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()
    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return

    admin_text = await format_admin_deal_review_text(deal, db_session)
    await db_session.commit()

    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="☑️ اعتماد وإطلاق الضمان", callback_data=f"ask_rel_{deal.id}")],
        [InlineKeyboardButton(text="🔍 فحص تفصيلي للصفقة", callback_data=f"adm_inspect_deal_{deal.id}")]
    ])
    try:
        await callback.message.edit_text(admin_text, reply_markup=admin_kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(admin_text, reply_markup=admin_kb, parse_mode="Markdown")
    await callback.answer()


@settlement_router.callback_query(F.data.startswith("man_rel_"))
async def manual_admin_release(callback: CallbackQuery, db_session: AsyncSession):
    if callback.from_user.id not in settings.ADMIN_IDS:
        await callback.answer("غير مصرح: هذه الصلاحية للإدارة فقط.", show_alert=True)
        return

    deal_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(
        select(Deal).where(Deal.id == deal_id).with_for_update()
    )
    deal = result.scalar_one_or_none()

    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return

    if deal.status == "COMPLETED":
        await callback.answer("تم اعتماد وإطلاق هذه الصفقة مسبقاً.", show_alert=True)
        return

    if deal.status != "ADMIN_APPROVAL":
        await callback.answer("لا يمكن إطلاق هذه الصفقة — لم تصل بعد لمرحلة الاعتماد الإداري.", show_alert=True)
        return

    # Strict gate: Buyer payout address must be valid before release
    buyer_addr = (deal.buyer_payout_address or "").strip()
    if not buyer_addr or buyer_addr in ["PENDING", "غير محدد", "N/A"]:
        await callback.answer("⚠️ لا يمكن الإطلاق: عنوان محفظة المشتري غير مسجل بعد. يجب انتظار إدخاله أولاً.", show_alert=True)
        return

    # Strict gate: Seller payment info must be valid before release
    seller_info_val = ""
    if isinstance(deal.seller_payment_info, dict):
        seller_info_val = str(deal.seller_payment_info.get("info") or "").strip()
    elif isinstance(deal.seller_payment_info, str):
        seller_info_val = deal.seller_payment_info.strip()

    if not seller_info_val or seller_info_val in ["PENDING", "غير محدد", "N/A"]:
        await callback.answer("⚠️ لا يمكن الإطلاق: بيانات حساب استلام البائع غير مسجلة بعد.", show_alert=True)
        return

    deal.status = "COMPLETED"

    # Increment completed_deals_count for both buyer and seller
    buyer_user_res = await db_session.execute(select(User).where(User.telegram_id == deal.buyer_id))
    buyer_user = buyer_user_res.scalar_one_or_none()
    if buyer_user:
        buyer_user.completed_deals_count = (buyer_user.completed_deals_count or 0) + 1

    seller_user_res = await db_session.execute(select(User).where(User.telegram_id == deal.seller_id))
    seller_user = seller_user_res.scalar_one_or_none()
    if seller_user:
        seller_user.completed_deals_count = (seller_user.completed_deals_count or 0) + 1

    offer_result = await db_session.execute(select(Offer).where(Offer.id == deal.offer_id))
    offer = offer_result.scalar_one_or_none()
    if offer and offer.status == "reserved":
        offer.status = "completed"

    seller_fee = getattr(deal, "seller_fee_usdt", None) or Decimal("0")
    buyer_fee = getattr(deal, "buyer_fee_usdt", None) or Decimal("0")

    add_audit(
        session=db_session,
        action="RELEASE_ESCROW",
        target_type="deal",
        target_id=deal.id,
        actor_id=callback.from_user.id,
        actor_type="ADMIN",
        details={
            "amount_usdt": str(deal.amount_usdt),
            "amount_syp": str(deal.amount_syp),
            "fee_usdt": str(deal.fee_usdt),
            "seller_fee_usdt": str(seller_fee),
            "buyer_fee_usdt": str(buyer_fee),
            "seller_id": deal.seller_id,
            "buyer_id": deal.buyer_id,
            "buyer_payout_address": deal.buyer_payout_address
        }
    )

    await db_session.commit()

    if offer:
        try:
            await update_channel_offer_message(callback.bot, offer)
        except Exception as e:
            logger.warning(f"Failed to update channel message after release for deal #{deal.id}: {e}")

    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)
    net_buyer_usdt = deal.amount_usdt - buyer_fee
    net_buyer_usdt_str = format_usdt(net_buyer_usdt)

    try:
        await callback.message.edit_text(
            f"✅ **صفقة #{deal.id} — تم الاعتماد والإطلاق بنجاح**\n\n"
            f"▫️ **قيمة الصفقة:** `{usdt_str} USDT`\n"
            f"▫️ **المبلغ بالليرة:** `{syp_str}` ل.س\n"
            f"▫️ **صافي المشتري المحول:** `{net_buyer_usdt_str} USDT`\n"
            f"▫️ **الشبكة:** `BEP-20`\n"
            f"📥 **محفظة المستلم:**\n`{escape_markdown(deal.buyer_payout_address)}`\n"
            f"▫️ **معتمد التنفيذ:** `{callback.from_user.id}`\n"
            f"▫️ **الحالة النهائية:** 🏁 مكتملة",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to edit admin message after release for deal #{deal.id}: {e}")

    # Synchronize Deal Cards for counterparties
    try:
        await push_deal_card(deal.id, callback.bot, notify_handoff=False)
    except Exception as e:
        logger.warning(f"Failed to push deal card on release for deal #{deal.id}: {e}")

    # Notify Buyer and Seller of completion
    try:
        await callback.bot.send_message(
            chat_id=deal.buyer_id,
            text=(
                f"✅ **تم إتمام صفقة #{deal.id} بنجاح**\n"
                f"〰️〰️〰️〰️〰️〰️〰️\n"
                f"💵 الرصيد المرسل : `{net_buyer_usdt_str} USDT` (`BEP-20`)\n"
                f"📥 المحفظة : `{escape_markdown(deal.buyer_payout_address)}`"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify buyer {deal.buyer_id}: {e}")

    try:
        await callback.bot.send_message(
            chat_id=deal.seller_id,
            text=(
                f"✅ **تم إتمام صفقة #{deal.id} بنجاح**\n"
                f"〰️〰️〰️〰️〰️〰️〰️\n"
                f"💵 الكمية : `{usdt_str} USDT`\n"
                f"💰 المبلغ المستلم : `{syp_str}` ل.س"
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify seller {deal.seller_id}: {e}")

    # Send one-tap rating prompts to both Buyer and Seller
    try:
        buyer_rate_text, buyer_rate_kb = build_deal_rating_prompt(deal.id, "البائع")
        await callback.bot.send_message(
            chat_id=deal.buyer_id,
            text=buyer_rate_text,
            reply_markup=buyer_rate_kb,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to send rating prompt to buyer {deal.buyer_id} for deal #{deal.id}: {e}")

    try:
        seller_rate_text, seller_rate_kb = build_deal_rating_prompt(deal.id, "المشتري")
        await callback.bot.send_message(
            chat_id=deal.seller_id,
            text=seller_rate_text,
            reply_markup=seller_rate_kb,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to send rating prompt to seller {deal.seller_id} for deal #{deal.id}: {e}")


# ================= POST-DEAL STAR RATING =================

@settlement_router.callback_query(F.data.startswith("rate_deal_"))
async def handle_deal_rating(callback: CallbackQuery, db_session: AsyncSession):
    parts = callback.data.split("_")
    if len(parts) != 4 or not parts[2].isdigit() or not parts[3].isdigit():
        await callback.answer("بيانات التقييم غير صالحة.", show_alert=True)
        return

    deal_id = int(parts[2])
    stars = int(parts[3])
    if stars < 1 or stars > 5:
        await callback.answer("يجب أن يكون التقييم بين 1 و 5 نجوم.", show_alert=True)
        return

    rater_id = callback.from_user.id
    deal_res = await db_session.execute(
        select(Deal).where(Deal.id == deal_id)
    )
    deal = deal_res.scalar_one_or_none()
    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return

    if deal.status != "COMPLETED":
        await callback.answer("يمكن التقييم فقط بعد إتمام الصفقة بنجاح.", show_alert=True)
        return

    if rater_id not in (deal.buyer_id, deal.seller_id):
        await callback.answer("غير مصرح: هذا التقييم مخصص لأطراف الصفقة فقط.", show_alert=True)
        return

    target_id = deal.seller_id if rater_id == deal.buyer_id else deal.buyer_id

    existing_res = await db_session.execute(
        select(DealRating).where(
            DealRating.deal_id == deal_id,
            DealRating.rater_id == rater_id
        ).with_for_update()
    )
    existing_rating = existing_res.scalar_one_or_none()
    if existing_rating:
        stars_vis = format_stars(existing_rating.stars)
        try:
            await callback.message.edit_text(
                f"✅ **تم تسجيل تقييمك لصفقة #{deal_id} مسبقاً**\n"
                f"〰️〰️〰️〰️〰️〰️〰️\n"
                f"⭐ **التقييم المسجل:** {stars_vis} (`{existing_rating.stars}/5`)",
                reply_markup=None,
                parse_mode="Markdown"
            )
        except Exception:
            pass
        await callback.answer("تم تسجيل تقييمك لهذه الصفقة مسبقاً.", show_alert=True)
        return

    new_rating = DealRating(
        deal_id=deal.id,
        rater_id=rater_id,
        target_id=target_id,
        stars=stars
    )
    db_session.add(new_rating)
    await db_session.flush()

    new_rep, total_ratings = await recalculate_user_reputation(db_session, target_id)

    add_audit(
        session=db_session,
        action="RATE_DEAL",
        target_type="deal",
        target_id=deal.id,
        actor_id=rater_id,
        actor_type="USER",
        details={
            "rater_id": rater_id,
            "target_id": target_id,
            "stars": stars,
            "new_reputation_score": str(new_rep),
            "total_ratings": total_ratings
        }
    )
    await db_session.commit()

    stars_vis = format_stars(stars)
    label = STAR_LABELS.get(stars, "")
    try:
        await callback.message.edit_text(
            f"✅ **شكراً لك! تم تسجيل تقييمك لصفقة #{deal.id}**\n"
            f"〰️〰️〰️〰️〰️〰️〰️\n"
            f"⭐ **التقييم المسجل:** {stars_vis} ({label} — `{stars}/5`)",
            reply_markup=None,
            parse_mode="Markdown"
        )
    except Exception:
        pass
    await callback.answer("شكراً لتقييمك!")

