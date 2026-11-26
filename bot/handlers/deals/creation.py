from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import Optional
import logging

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from db.models import Deal, Offer, User
from bot.states.flow_states import DealCreateFSM
from bot.services.audit_service import record_audit
from bot.services.channel import update_channel_offer_message
from bot.utils.deal_card import push_deal_card
from bot.utils.deal_guard import (
    MAX_ACTIVE_DEALS,
    count_user_active_deals,
    build_max_active_deals_kb,
    build_max_active_deals_notice_text,
    notify_offer_owner_max_deals,
)
from bot.utils.fees import calculate_deal_fee
from bot.utils.branding import BOT_NAME
from bot.utils.formatters import (
    format_payment_methods,
    format_syp,
    format_usdt,
    clean_numeric_input,
    PAYMENT_METHOD_ICONS,
    MIN_DEAL_AMOUNT_USDT,
)
from bot.utils.transitions import can_user_interact_with_offer

logger = logging.getLogger(__name__)

creation_router = Router(name="deals_creation")


@creation_router.callback_query(F.data.startswith("start_deal_"))
async def init_deal(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    # Check if user is banned
    user_res = await db_session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = user_res.scalar_one_or_none()
    if user and user.is_banned:
        await callback.answer(f"🚫 حسابك محظور من بدء أي صفقات جديدة على {BOT_NAME}.", show_alert=True)
        return

    # Section 6.1.1: Active deals cap check for applicant (max 5 active deals)
    user_id = callback.from_user.id
    active_deals_count = await count_user_active_deals(db_session, user_id)
    if active_deals_count >= MAX_ACTIVE_DEALS:
        await callback.answer(
            "لديك 5 صفقات غير مكتملة يرجى متابعتهم (إلغاء أو استكمال) لتتمكن من إجراء معاملات جديدة.",
            show_alert=True
        )
        await callback.message.answer(
            build_max_active_deals_notice_text(),
            reply_markup=build_max_active_deals_kb(),
            parse_mode="Markdown",
        )
        return

    offer_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(
        select(Offer).where(Offer.id == offer_id)
    )
    offer = result.scalar_one_or_none()

    if not offer:
        await callback.answer("عذراً، لم يتم العثور على العرض.", show_alert=True)
        return

    allowed, err_msg = can_user_interact_with_offer(offer.user_id, callback.from_user.id)
    if not allowed:
        await callback.answer(err_msg, show_alert=True)
        return

    if offer.status != "active":
        await callback.answer("عذراً، هذا العرض لم يعد متاحاً للتداول حالياً.", show_alert=True)
        return

    # Section 6.1.1: Active deals cap check for offer owner (max 5 active deals)
    if offer.user_id:
        owner_active_deals = await count_user_active_deals(db_session, offer.user_id)
        if owner_active_deals >= MAX_ACTIVE_DEALS:
            await notify_offer_owner_max_deals(callback.bot, offer.user_id, offer.id)
            await callback.answer(
                "⚠️ عذراً، لا يمكن التقديم على هذا العرض حالياً لأن صاحب العرض لديه 5 صفقات غير مكتملة.",
                show_alert=True,
            )
            return

    await state.update_data(
        offer_id=offer.id,
        rate=str(offer.rate),
        offer_type=offer.offer_type,
        creator_id=offer.user_id,
        payout_info=offer.payout_info,
        payout_details=offer.payout_details or {},
        payment_methods=list(offer.payment_methods) if offer.payment_methods else [],
        min_limit=str(offer.min_limit),
        max_limit=str(offer.max_limit)
    )

    offer_type_ar = "🔴 بيع" if offer.offer_type == 'sell' else "🟢 شراء"
    taker_role_ar = "🟢 مشتري" if offer.offer_type == 'sell' else "🔴 بائع"
    rate_str = format_syp(offer.rate)
    min_str = format_usdt(offer.min_limit)
    max_str = format_usdt(offer.max_limit)
    methods_str = format_payment_methods(offer.payment_methods)

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])

    await callback.message.answer(
        f"📝 **طلب تنفيذ عرض**\n"
        f"🔢 **رقم العرض :** {offer.id}\n"
        f"⚖️ **النوع :** {offer_type_ar}\n"
        f"👤 **دورك :** {taker_role_ar}\n"
        f"💵 **السعر :** `{rate_str}` ل.س\n"
        f"💰 **الكمية المتاحة :** من `{min_str}` إلى `{max_str} USDT`\n"
        f"💳 **طريقة الدفع :** {methods_str}\n\n"
        f"⌨️ **أرسل الكمية المطلوبة (بـ `USDT`) فقط:**",
        reply_markup=cancel_kb,
        parse_mode="Markdown"
    )
    await state.set_state(DealCreateFSM.waiting_for_amount)
    await callback.answer()


@creation_router.message(DealCreateFSM.waiting_for_amount)
async def process_deal_amount(message: Message, state: FSMContext, db_session: AsyncSession):
    data = await state.get_data()
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    try:
        amount_usdt = clean_numeric_input(message.text)
        if not amount_usdt or amount_usdt <= 0 or amount_usdt > Decimal("999999999"):
            raise ValueError("Invalid amount")
    except Exception:
        await message.answer(
            "⚠️ **يرجى إرسال رقم صحيح للكمية المطلوبة (بـ `USDT`):**",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        return

    if amount_usdt < MIN_DEAL_AMOUNT_USDT:
        await message.answer(
            "⚠️ أقل كمية مسموحة هي 1 USDT.",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        return

    min_limit = Decimal(data['min_limit'])
    max_limit = Decimal(data['max_limit'])

    min_str = format_usdt(min_limit)
    max_str = format_usdt(max_limit)

    if amount_usdt < min_limit or amount_usdt > max_limit:
        await message.answer(
            f"⚠️ **الكمية خارج النطاق المتاح** (`{min_str}` - `{max_str} USDT`).\n"
            f"أرسل كمية ضمن الحدود:",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        return

    rate = Decimal(data['rate'])
    amount_syp = (amount_usdt * rate).quantize(Decimal("0.01"))
    if amount_syp < Decimal("1"):
        await message.answer(
            "⚠️ أقل قيمة إجمالية مسموحة للصفقة هي 1 ل.س.",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        return
    data['amount_usdt'] = str(amount_usdt)
    data['amount_syp'] = str(amount_syp)
    await state.update_data(amount_usdt=str(amount_usdt), amount_syp=str(amount_syp))

    methods = data.get("payment_methods") or []
    offer_type = data.get('offer_type', 'sell')
    offer_id = data.get('offer_id')

    # If the offer supports more than one payment method, let the counterparty choose their preferred method
    if len(methods) > 1:
        buttons = []
        for idx, m in enumerate(methods):
            icon = PAYMENT_METHOD_ICONS.get(m, "💳")
            buttons.append([InlineKeyboardButton(text=f"{icon} {m}", callback_data=f"deal_select_pm_{idx}")])
        buttons.append([
            InlineKeyboardButton(text="🔙 رجوع", callback_data="deal_back_to_amount"),
            InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")
        ])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        await state.set_state(DealCreateFSM.waiting_for_payment_method)
        prompt_msg = (
            f"🔢 **الكمية المطلوبة :** `{format_usdt(amount_usdt)} USDT`\n"
            f"💴 **المبلغ المقابل :** `{format_syp(amount_syp)}` ل.س\n\n"
            f"💳 **اختر طريقة الدفع:**"
        )
        await message.answer(prompt_msg, reply_markup=kb, parse_mode="Markdown")
        return

    # Single payment method or default
    selected_pm = methods[0] if methods else "شام كاش ليرة"
    creator_id = data.get('creator_id')
    taker_id = message.from_user.id

    if offer_type == 'buy':
        seller_id = taker_id
        buyer_id = creator_id
    else:
        seller_id = creator_id
        buyer_id = taker_id

    await _finalize_deal_creation(
        message=message,
        state=state,
        db_session=db_session,
        data=data,
        seller_id=seller_id,
        buyer_id=buyer_id,
        buyer_payout_address="PENDING",
        seller_payment_info={"method": selected_pm, "info": "PENDING"},
        actor_id=taker_id,
    )


@creation_router.callback_query(
    DealCreateFSM(),
    F.data == "deal_back_to_amount"
)
async def back_to_deal_amount(callback: CallbackQuery, state: FSMContext):
    """Return user back to deal amount input screen."""
    data = await state.get_data()
    offer_id = data.get("offer_id", "")
    offer_type = data.get("offer_type", "sell")
    offer_type_ar = "🔴 بيع" if offer_type == "sell" else "🟢 شراء"
    taker_role_ar = "🟢 مشتري" if offer_type == "sell" else "🔴 بائع"
    rate_str = format_syp(Decimal(str(data.get("rate", "0"))))
    min_str = format_usdt(Decimal(str(data.get("min_limit", "0"))))
    max_str = format_usdt(Decimal(str(data.get("max_limit", "0"))))
    methods_str = format_payment_methods(data.get("payment_methods", []))

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    text = (
        f"📝 **طلب تنفيذ عرض**\n"
        f"🔢 **رقم العرض :** {offer_id}\n"
        f"⚖️ **النوع :** {offer_type_ar}\n"
        f"👤 **دورك :** {taker_role_ar}\n"
        f"💵 **السعر :** `{rate_str}` ل.س\n"
        f"💰 **الكمية المتاحة :** من `{min_str}` إلى `{max_str} USDT`\n"
        f"💳 **طريقة الدفع :** {methods_str}\n\n"
        f"⌨️ **أرسل الكمية المطلوبة (بـ `USDT`) فقط:**"
    )
    await state.set_state(DealCreateFSM.waiting_for_amount)
    try:
        await callback.message.edit_text(text, reply_markup=cancel_kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=cancel_kb, parse_mode="Markdown")
    await callback.answer()


@creation_router.callback_query(DealCreateFSM.waiting_for_payment_method, F.data.startswith("deal_select_pm_"))
async def process_deal_payment_method_choice(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    idx_str = callback.data.split("_")[-1]
    data = await state.get_data()
    methods = data.get("payment_methods", [])
    try:
        idx = int(idx_str)
        selected_pm = methods[idx]
    except (ValueError, IndexError):
        await callback.answer("⚠️ اختيار غير صالح.", show_alert=True)
        return

    await state.update_data(selected_payment_method=selected_pm)
    data['selected_payment_method'] = selected_pm

    offer_type = data.get('offer_type', 'sell')
    creator_id = data.get('creator_id')
    taker_id = callback.from_user.id

    if offer_type == 'buy':
        seller_id = taker_id
        buyer_id = creator_id
    else:
        seller_id = creator_id
        buyer_id = taker_id

    await callback.answer()
    await _finalize_deal_creation(
        message=callback.message,
        state=state,
        db_session=db_session,
        data=data,
        seller_id=seller_id,
        buyer_id=buyer_id,
        buyer_payout_address="PENDING",
        seller_payment_info={"method": selected_pm, "info": "PENDING"},
        bot=callback.bot,
        actor_id=taker_id,
    )


async def derive_escrow_address(db_session: AsyncSession) -> tuple[str, int]:
    """Derive escrow wallet address and index for the deal using PostgreSQL SEQUENCE."""
    result = await db_session.execute(text("SELECT nextval('deposit_index_seq')"))
    deposit_index = int(result.scalar_one())
    deposit_address = f"0x_mock_{deposit_index}"
    return deposit_address, deposit_index


async def _finalize_deal_creation(
    message: Message,
    state: FSMContext,
    db_session: AsyncSession,
    data: dict,
    seller_id: int,
    buyer_id: int,
    buyer_payout_address: str,
    seller_payment_info: dict,
    bot: Optional[Bot] = None,
    actor_id: Optional[int] = None,
):
    if bot is None:
        bot = message.bot
    taker_user_id = actor_id if actor_id is not None else message.from_user.id
    offer_id = data['offer_id']
    amount_usdt_val = data.get('amount_usdt')
    if not amount_usdt_val:
        fresh_data = await state.get_data()
        amount_usdt_val = fresh_data.get('amount_usdt') or '0'
        data['amount_usdt'] = amount_usdt_val
        data['amount_syp'] = fresh_data.get('amount_syp') or '0'

    amount_usdt = Decimal(str(amount_usdt_val))
    amount_syp = Decimal(str(data.get('amount_syp', '0')))

    now = datetime.now(timezone.utc)

    # Re-verify offer availability under FOR UPDATE lock
    offer_res = await db_session.execute(
        select(Offer).where(Offer.id == offer_id).with_for_update()
    )
    offer = offer_res.scalar_one_or_none()

    if not offer or offer.status != "active" or (offer.expires_at and offer.expires_at <= now):
        await db_session.commit()
        await state.clear()
        await message.answer("⚠️ عذراً، هذا العرض لم يعد متاحاً للتداول حالياً.")
        return

    allowed, err_msg = can_user_interact_with_offer(offer.user_id, taker_user_id)
    if not allowed or seller_id == buyer_id:
        await db_session.commit()
        await state.clear()
        await message.answer(f"⚠️ {err_msg}")
        return

    taker_active_deals = await count_user_active_deals(db_session, taker_user_id)
    if taker_active_deals >= MAX_ACTIVE_DEALS:
        await db_session.commit()
        await state.clear()
        await message.answer(
            build_max_active_deals_notice_text(),
            reply_markup=build_max_active_deals_kb(),
            parse_mode="Markdown",
        )
        return

    if offer.user_id:
        owner_active_deals = await count_user_active_deals(db_session, offer.user_id)
        if owner_active_deals >= MAX_ACTIVE_DEALS:
            await db_session.commit()
            await state.clear()
            await notify_offer_owner_max_deals(bot, offer.user_id, offer.id)
            await message.answer(
                "⚠️ **عذراً، لا يمكن التقديم على هذا العرض حالياً**\n\n"
                f"صاحب هذا العرض لديه حالياً `{MAX_ACTIVE_DEALS}` صفقات غير مكتملة (الحد الأقصى المسموح به).\n"
                "تم إرسال إشعار لصاحب العرض لمتابعة صفقاته، ويمكنك المحاولة لاحقاً أو تصفح العروض الأخرى في السوق.",
                parse_mode="Markdown",
            )
            return

    min_limit_db = Decimal(str(offer.min_limit))
    max_limit_db = Decimal(str(offer.max_limit))
    if amount_usdt < MIN_DEAL_AMOUNT_USDT:
        await db_session.commit()
        await state.clear()
        await message.answer("⚠️ أقل كمية مسموحة هي 1 USDT.")
        return

    if amount_usdt < min_limit_db or amount_usdt > max_limit_db:
        await db_session.commit()
        await state.clear()
        await message.answer("⚠️ عذراً، الكمية المطلوبة أصبحت خارج حدود العرض الحالية.")
        return

    amount_syp = (amount_usdt * Decimal(str(offer.rate))).quantize(Decimal("0.01"))
    if amount_syp < Decimal("1"):
        await db_session.commit()
        await state.clear()
        await message.answer("⚠️ أقل قيمة إجمالية مسموحة للصفقة هي 1 ل.س.")
        return

    # Percentage split fee calculation
    fee_breakdown = calculate_deal_fee(amount_usdt)
    total_fee_usdt = fee_breakdown.total_fee
    seller_fee_usdt = fee_breakdown.seller_fee
    buyer_fee_usdt = fee_breakdown.buyer_fee

    # Generate sequential BEP20 Escrow Address safely
    deposit_address, deposit_index = await derive_escrow_address(db_session)

    # Create Deal cleanly inside same transaction using unified PENDING_OWNER_APPROVAL status
    new_deal = Deal(
        offer_id=offer_id,
        seller_id=seller_id,
        buyer_id=buyer_id,
        amount_usdt=amount_usdt,
        amount_syp=amount_syp,
        fee_usdt=total_fee_usdt,
        seller_fee_usdt=seller_fee_usdt,
        buyer_fee_usdt=buyer_fee_usdt,
        deposit_address=deposit_address,
        deposit_index=deposit_index,
        buyer_payout_address=buyer_payout_address,
        seller_payment_info=seller_payment_info,
        status="PENDING_OWNER_APPROVAL",
        expires_at=now + timedelta(minutes=30)
    )
    db_session.add(new_deal)

    # Mark Offer as reserved
    offer.status = "reserved"
    offer.status_changed_at = now

    try:
        await db_session.commit()
        await db_session.refresh(new_deal)
    except IntegrityError:
        await db_session.rollback()
        await state.clear()
        await message.answer("⚠️ عذراً، هذا العرض محجوز حالياً أو قيد التفاوض من قبل متداول آخر.")
        return

    await state.clear()

    # Audit log for deal creation
    try:
        await record_audit(
            session=db_session,
            action="CREATE_DEAL",
            target_type="deal",
            target_id=new_deal.id,
            actor_id=taker_user_id,
            actor_type="USER",
            details={
                "offer_id": offer_id,
                "seller_id": seller_id,
                "buyer_id": buyer_id,
                "amount_usdt": str(amount_usdt),
                "amount_syp": str(amount_syp)
            }
        )
    except Exception as e:
        logger.warning(f"Failed to record audit for deal #{new_deal.id}: {e}")

    # Update channel offer message immediately to reflect reserved status
    try:
        await update_channel_offer_message(bot, offer)
    except Exception as e:
        logger.warning(f"Failed to update channel offer message for reserved offer #{offer.id}: {e}")

    # Dispatch unified deal card to both maker and taker
    await push_deal_card(new_deal.id, bot)
