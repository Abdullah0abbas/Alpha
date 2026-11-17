import logging
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_, and_, func
from sqlalchemy.exc import IntegrityError

from aiogram import Router, F, Bot
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext

from db.models import Offer, Negotiation, User, Deal
from bot.states.flow_states import NegotiationFSM
from bot.utils.formatters import (
    PAYMENT_METHOD_ICONS,
    format_syp,
    format_usdt,
    clean_numeric_input
)
from bot.services.channel import update_channel_offer_message
from config.settings import settings
from bot.utils.branding import BOT_NAME
from bot.utils.deal_card import push_deal_card
from bot.utils.fees import calculate_deal_fee
from bot.utils.payment_prompts import extract_clean_account

from bot.utils.transitions import can_user_interact_with_offer

logger = logging.getLogger(__name__)

negotiations_router = Router()

# ================= 1. START NEGOTIATION (TAKER INITIATES) =================

@negotiations_router.callback_query(F.data.startswith("start_neg_"))
async def init_negotiation(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    # Check if user is banned
    user_res = await db_session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = user_res.scalar_one_or_none()
    if user and user.is_banned:
        await callback.answer(f"🚫 حسابك محظور من بدء أي مفاوضات على {BOT_NAME}.", show_alert=True)
        return

    offer_id = int(callback.data.split("_")[-1])
    taker_id = callback.from_user.id

    # Section 6.1.1: Active deals cap check (max 5 active deals)
    if taker_id not in settings.ADMIN_IDS:
        deals_count_res = await db_session.execute(
            select(func.count(Deal.id)).where(
                or_(Deal.seller_id == taker_id, Deal.buyer_id == taker_id),
                Deal.status.not_in(['COMPLETED', 'CANCELLED', 'EXPIRED'])
            )
        )
        active_deals_count = deals_count_res.scalar() or 0
        if active_deals_count >= 5:
            await callback.answer(
                "⚠️ لديك 5 صفقات نشطة بالفعل. يرجى إكمال أو إلغاء إحدى صفقاتك السابقة قبل بدء تفاوض جديد.",
                show_alert=True
            )
            return

    result = await db_session.execute(
        select(Offer).where(Offer.id == offer_id)
    )
    offer = result.scalar_one_or_none()

    if not offer:
        await callback.answer("عذراً، لم يتم العثور على العرض.", show_alert=True)
        return

    allowed, err_msg = can_user_interact_with_offer(offer.user_id, taker_id)
    if not allowed:
        await callback.answer(err_msg, show_alert=True)
        return

    if not offer.negotiable or offer.negotiable_type == "none":
        await callback.answer("عذراً، هذا العرض لا يقبل التفاوض.", show_alert=True)
        return

    if offer.status != "active":
        await callback.answer("عذراً، هذا العرض غير متاح حالياً (قيد التفاوض أو التنفيذ مع طرف آخر).", show_alert=True)
        return

    # Check if taker already attempted negotiation on this offer and was rejected or completed
    prev_neg_res = await db_session.execute(
        select(Negotiation).where(
            Negotiation.offer_id == offer_id,
            Negotiation.taker_id == taker_id,
            Negotiation.status.in_(["REJECTED", "ACCEPTED"])
        )
    )
    if prev_neg_res.scalars().first():
        await callback.answer("عذراً، يحق لكل مستخدم جولة تفاوض واحدة فقط على هذا العرض.", show_alert=True)
        return

    await state.update_data(
        offer_id=offer.id,
        maker_id=offer.user_id,
        taker_id=taker_id,
        offer_type=offer.offer_type,
        current_rate=str(offer.rate),
        min_limit=str(offer.min_limit),
        max_limit=str(offer.max_limit),
        negotiable_type=offer.negotiable_type,
        payout_info=offer.payout_info,
        payout_details=offer.payout_details or {},
        payment_methods=list(offer.payment_methods) if offer.payment_methods else []
    )

    rate_str = format_syp(offer.rate)
    min_str = format_usdt(offer.min_limit)
    max_str = format_usdt(offer.max_limit)
    neg_type = offer.negotiable_type

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])

    if neg_type in ["amount", "both"]:
        await callback.message.answer(
            f"🤝 **بدء جلسة تفاوض — عرض #{offer.id}**\n\n"
            f"▫️ **سعر الصرف الحالي:** `{rate_str}` ل.س\n"
            f"▫️ **حدود الكمية:** من `{min_str} USDT` إلى `{max_str} USDT`\n\n"
            f"⌨️ أدخل الكمية المقترحة للتداول (بـ `USDT`):",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        await state.set_state(NegotiationFSM.waiting_for_taker_amount)
    elif Decimal(str(offer.min_limit)) < Decimal(str(offer.max_limit)):
        await callback.message.answer(
            f"🤝 **بدء جلسة تفاوض على السعر — عرض #{offer.id}**\n\n"
            f"▫️ **سعر الصرف الحالي:** `{rate_str}` ل.س\n"
            f"▫️ **حدود الكمية:** من `{min_str} USDT` إلى `{max_str} USDT`\n\n"
            f"⌨️ أدخل أولاً الكمية التي ترغب بتداولها ضمن الحدود (بـ `USDT`):",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        await state.set_state(NegotiationFSM.waiting_for_taker_amount)
    else:
        await state.update_data(proposed_amount=str(offer.min_limit))
        await callback.message.answer(
            f"🤝 **بدء جلسة تفاوض على السعر — عرض #{offer.id}**\n\n"
            f"▫️ **الكمية الثابتة للعرض:** `{min_str} USDT`\n"
            f"▫️ **سعر الصرف الحالي:** `{rate_str}` ل.س\n\n"
            f"⌨️ أدخل سعر الصرف المقترح بالليرة السورية:",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        await state.set_state(NegotiationFSM.waiting_for_taker_rate)
    await callback.answer()


@negotiations_router.message(NegotiationFSM.waiting_for_taker_amount)
async def process_taker_amount(message: Message, state: FSMContext):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    try:
        amount_val = clean_numeric_input(message.text)
        if not amount_val or amount_val <= 0:
            await message.answer("⚠️ الكمية يجب أن تكون أكبر من 0. يرجى إدخال رقم صحيح:", reply_markup=cancel_kb, parse_mode="Markdown")
            return
    except Exception:
        await message.answer("⚠️ إدخال غير صحيح. يرجى إدخال رقم صحيح للكمية:", reply_markup=cancel_kb, parse_mode="Markdown")
        return

    data = await state.get_data()
    min_limit = Decimal(str(data.get("min_limit", "0")))
    max_limit = Decimal(str(data.get("max_limit", "0")))
    if (min_limit > 0 and amount_val < min_limit) or (max_limit > 0 and amount_val > max_limit):
        await message.answer(
            "⚠️ **الكمية خارج حدود العرض**\n\n"
            f"▫️ **الحدود المتاحة:** من `{format_usdt(min_limit)} USDT` إلى `{format_usdt(max_limit)} USDT`\n\n"
            "يرجى إدخال كمية ضمن هذا النطاق:",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        return

    await state.update_data(proposed_amount=str(amount_val))

    if data.get("negotiable_type") in ("both", "rate"):
        rate_str = format_syp(Decimal(data['current_rate']))
        await message.answer(
            f"▫️ **الكمية المقترحة:** `{format_usdt(amount_val)} USDT`\n"
            f"▫️ **سعر الصرف الحالي:** `{rate_str}` ل.س\n\n"
            "⌨️ أدخل الآن سعر الصرف المقترح بالليرة السورية:",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        await state.set_state(NegotiationFSM.waiting_for_taker_rate)
    else:
        proposed_rate = Decimal(data['current_rate'])
        await _submit_taker_negotiation(message, state, amount_val, proposed_rate)


@negotiations_router.message(NegotiationFSM.waiting_for_taker_rate)
async def process_taker_rate(message: Message, state: FSMContext):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    try:
        rate_val = clean_numeric_input(message.text)
        if not rate_val or rate_val <= 0:
            await message.answer("⚠️ السعر يجب أن يكون أكبر من 0. يرجى إدخال رقم صحيح:", reply_markup=cancel_kb, parse_mode="Markdown")
            return
    except Exception:
        await message.answer("⚠️ إدخال غير صحيح. يرجى إدخال رقم لسعر الصرف:", reply_markup=cancel_kb, parse_mode="Markdown")
        return

    data = await state.get_data()
    amount_val = Decimal(data.get("proposed_amount", data.get("min_limit", "10")))
    await _submit_taker_negotiation(message, state, amount_val, rate_val)


async def _submit_taker_negotiation(message: Message, state: FSMContext, amount_val: Decimal, rate_val: Decimal):
    data = await state.get_data()
    offer_id = data['offer_id']
    maker_id = data['maker_id']
    taker_id = message.from_user.id

    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db_session:
        now = datetime.now(timezone.utc)

        # Re-verify offer under FOR UPDATE lock
        offer_res = await db_session.execute(
            select(Offer).where(Offer.id == offer_id).with_for_update()
        )
        offer = offer_res.scalar_one_or_none()

        if not offer or offer.status != "active" or (offer.expires_at and offer.expires_at <= now):
            await db_session.commit()
            await state.clear()
            await message.answer("⚠️ عذراً، هذا العرض لم يعد متاحاً للتفاوض حالياً.")
            return

        min_limit = Decimal(str(offer.min_limit))
        max_limit = Decimal(str(offer.max_limit))
        if amount_val < min_limit or amount_val > max_limit:
            await db_session.commit()
            await state.clear()
            await message.answer("⚠️ عذراً، الكمية المقترحة أصبحت خارج حدود العرض الحالية.")
            return

        # Lock offer to negotiating and update status_changed_at atomically
        offer.status = "negotiating"
        offer.status_changed_at = now

        expires_at = now + timedelta(minutes=30)

        neg = Negotiation(
            offer_id=offer_id,
            taker_id=taker_id,
            maker_id=maker_id,
            proposed_rate=rate_val,
            proposed_amount=amount_val,
            status="PENDING_MAKER",
            round=1,
            expires_at=expires_at
        )
        try:
            db_session.add(neg)
            await db_session.commit()
            await db_session.refresh(neg)
        except IntegrityError:
            await db_session.rollback()
            await state.clear()
            await message.answer("⚠️ عذراً، هذا العرض لم يعد متاحاً للتفاوض حالياً.")
            return

        # Update channel offer message post-commit
        try:
            await update_channel_offer_message(message.bot, offer)
        except Exception as e:
            logger.warning(f"Failed to update channel message for negotiation #{neg.id}: {e}")

        # Get Taker stats
        taker_deals_res = await db_session.execute(
            select(Deal).where(
                or_(Deal.buyer_id == taker_id, Deal.seller_id == taker_id),
                Deal.status == "COMPLETED"
            )
        )
        taker_completed_count = len(taker_deals_res.scalars().all())

    await state.clear()

    total_syp = amount_val * rate_val
    amount_str = format_usdt(amount_val)
    rate_str = format_syp(rate_val)
    total_syp_str = format_syp(total_syp)

    await message.answer(
        f"📨 **تم إرسال طلب التفاوض — عرض #{offer_id}**\n\n"
        f"▫️ **الكمية المقترحة:** `{amount_str} USDT`\n"
        f"▫️ **سعر الصرف المقترح:** `{rate_str}` ل.س\n"
        f"▫️ **القيمة الإجمالية:** `{total_syp_str}` ل.س\n"
        f"▫️ **مهلة الرد:** 30 دقيقة\n\n"
        "تم إرسال المقترح إلى صاحب العرض، وسيصلك إشعار فور اتخاذ قراره.",
        parse_mode="Markdown"
    )

    maker_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ قبول", callback_data=f"neg_m_acc_{neg.id}"),
            InlineKeyboardButton(text="❌ رفض", callback_data=f"neg_m_rej_{neg.id}")
        ],
        [
            InlineKeyboardButton(text="🔄 تقديم عرض مضاد", callback_data=f"neg_m_cnt_{neg.id}")
        ]
    ])

    maker_text = (
        f"🔔 **طلب تفاوض جديد على عرضك #{offer_id}**\n\n"
        f"▫️ **الكمية المقترحة:** `{amount_str} USDT`\n"
        f"▫️ **السعر المقترح:** `{rate_str}` ل.س\n"
        f"▫️ **القيمة الإجمالية:** `{total_syp_str}` ل.س\n"
        f"▫️ **سجل الطرف المفاوض:** `{taker_completed_count}` صفقة مكتملة\n"
        f"▫️ **مهلة الرد:** 30 دقيقة\n\n"
        "اختر الإجراء المناسب:"
    )

    try:
        await message.bot.send_message(
            chat_id=maker_id,
            text=maker_text,
            reply_markup=maker_kb,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify maker {maker_id} of negotiation: {e}")


# ================= 2. MAKER DECISION (ACCEPT / REJECT / COUNTER) =================

@negotiations_router.callback_query(F.data.startswith("neg_m_acc_"))
async def maker_accept_negotiation(callback: CallbackQuery, db_session: AsyncSession, state: FSMContext):
    neg_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(
        select(Negotiation).where(Negotiation.id == neg_id).with_for_update()
    )
    neg = result.scalar_one_or_none()

    if not neg:
        await callback.answer("عذراً، لم يتم العثور على جلسة التفاوض.", show_alert=True)
        return

    if callback.from_user.id != neg.maker_id and callback.from_user.id not in settings.ADMIN_IDS:
        await callback.answer("غير مصرح: هذا الإجراء لصاحب العرض فقط.", show_alert=True)
        return

    if neg.status != "PENDING_MAKER":
        await db_session.commit()
        await callback.answer("تم اتخاذ قرار مسبقاً بشأن هذا التفاوض.", show_alert=True)
        return

    success = await _finalize_negotiated_deal(callback.bot, neg, db_session, callback.message)
    if success:
        await callback.answer("تم قبول شروط التفاوض بنجاح ✅")
    else:
        await callback.answer()


@negotiations_router.callback_query(F.data.startswith("neg_m_rej_"))
async def maker_reject_negotiation(callback: CallbackQuery, db_session: AsyncSession):
    neg_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(
        select(Negotiation).where(Negotiation.id == neg_id).with_for_update()
    )
    neg = result.scalar_one_or_none()

    if not neg:
        await callback.answer("عذراً، لم يتم العثور على جلسة التفاوض.", show_alert=True)
        return

    if callback.from_user.id != neg.maker_id and callback.from_user.id not in settings.ADMIN_IDS:
        await callback.answer("غير مصرح: هذا الإجراء لصاحب العرض فقط.", show_alert=True)
        return

    from bot.utils.transitions import can_maker_reject_negotiation
    if not can_maker_reject_negotiation(neg.status):
        await db_session.commit()
        await callback.answer("تم اتخاذ قرار مسبقاً بشأن هذا التفاوض.", show_alert=True)
        return

    neg.status = "REJECTED"
    
    # Revert offer to active
    offer_id_val = neg.offer_id
    reverted_offer = None
    if offer_id_val:
        offer_res = await db_session.execute(
            select(Offer).where(Offer.id == offer_id_val).with_for_update()
        )
        offer = offer_res.scalar_one_or_none()
        if offer and offer.status == "negotiating":
            offer.status = "active"
            offer.status_changed_at = datetime.now(timezone.utc)
            reverted_offer = offer

    await db_session.commit()

    if reverted_offer:
        try:
            await update_channel_offer_message(callback.bot, reverted_offer)
        except Exception as e:
            logger.warning(f"Failed to update channel message for reverted offer #{offer_id_val}: {e}")

    await callback.answer("تم رفض طلب التفاوض.")
    await callback.message.edit_text(
        f"❌ **تم رفض التفاوض — عرض #{neg.offer_id}**\n\n"
        "تم رفض طلب التفاوض وإعادة العرض فوراً إلى السوق كعرض نشط.",
        parse_mode="Markdown"
    )

    try:
        await callback.bot.send_message(
            chat_id=neg.taker_id,
            text=(
                f"ℹ️ **تحديث تفاوض عرض #{neg.offer_id}**\n\n"
                "اعتذر صاحب العرض عن قبول الشروط المقترحة.\n"
                "يمكنك استعراض باقي العروض المتاحة عبر /offers."
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify taker {neg.taker_id}: {e}")


@negotiations_router.callback_query(F.data.startswith("neg_m_cnt_"))
async def maker_start_counter(callback: CallbackQuery, db_session: AsyncSession, state: FSMContext):
    neg_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(select(Negotiation).where(Negotiation.id == neg_id))
    neg = result.scalar_one_or_none()

    if not neg:
        await callback.answer("عذراً، لم يتم العثور على جلسة التفاوض.", show_alert=True)
        return

    if callback.from_user.id != neg.maker_id and callback.from_user.id not in settings.ADMIN_IDS:
        await callback.answer("غير مصرح: هذا الإجراء لصاحب العرض فقط.", show_alert=True)
        return

    if neg.status != "PENDING_MAKER":
        await callback.answer("تم اتخاذ قرار مسبقاً بشأن هذا التفاوض.", show_alert=True)
        return

    offer_res = await db_session.execute(select(Offer).where(Offer.id == neg.offer_id))
    offer = offer_res.scalar_one_or_none()

    await state.update_data(
        neg_id=neg.id,
        offer_id=neg.offer_id,
        taker_id=neg.taker_id,
        maker_id=neg.maker_id,
        negotiable_type=offer.negotiable_type if offer else "both",
        current_rate=str(neg.proposed_rate),
        current_amount=str(neg.proposed_amount)
    )

    await callback.answer()

    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])

    neg_type = offer.negotiable_type if offer else "both"
    if neg_type in ["amount", "both"]:
        await callback.message.answer(
            f"🔄 **تقديم عرض مضاد — جلسة #{neg.id}**\n\n"
            f"▫️ **الكمية المقترحة حالياً:** `{format_usdt(neg.proposed_amount)} USDT`\n"
            f"▫️ **سعر الصرف المقترح حالياً:** `{format_syp(neg.proposed_rate)}` ل.س\n\n"
            "⌨️ أدخل الكمية النهائية التي تقترحها (بـ `USDT`):",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        await state.set_state(NegotiationFSM.waiting_for_maker_counter_amount)
    else:
        await state.update_data(counter_amount=str(neg.proposed_amount))
        await callback.message.answer(
            f"🔄 **تقديم عرض مضاد على السعر — جلسة #{neg.id}**\n\n"
            f"▫️ **سعر الصرف المقترح حالياً:** `{format_syp(neg.proposed_rate)}` ل.س\n\n"
            "⌨️ أدخل السعر النهائي الذي تقترحه بالليرة السورية:",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        await state.set_state(NegotiationFSM.waiting_for_maker_counter_rate)


@negotiations_router.message(NegotiationFSM.waiting_for_maker_counter_amount)
async def process_maker_counter_amount(message: Message, state: FSMContext):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    try:
        amount_val = clean_numeric_input(message.text)
        if not amount_val or amount_val <= 0:
            await message.answer("⚠️ الكمية يجب أن تكون أكبر من 0. يرجى إدخال رقم صحيح:", reply_markup=cancel_kb, parse_mode="Markdown")
            return
    except Exception:
        await message.answer("⚠️ إدخال غير صحيح. يرجى إدخال رقم صحيح للكمية:", reply_markup=cancel_kb, parse_mode="Markdown")
        return

    data = await state.get_data()
    await state.update_data(counter_amount=str(amount_val))

    if data.get("negotiable_type") == "both":
        await message.answer(
            f"▫️ **الكمية المسجلة:** `{format_usdt(amount_val)} USDT`\n\n"
            "⌨️ أدخل السعر النهائي المقترح بالليرة السورية:",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        await state.set_state(NegotiationFSM.waiting_for_maker_counter_rate)
    else:
        counter_rate = Decimal(data['current_rate'])
        await _submit_maker_counter(message, state, amount_val, counter_rate)


@negotiations_router.message(NegotiationFSM.waiting_for_maker_counter_rate)
async def process_maker_counter_rate(message: Message, state: FSMContext):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    try:
        rate_val = clean_numeric_input(message.text)
        if not rate_val or rate_val <= 0:
            await message.answer("⚠️ السعر يجب أن يكون أكبر من 0. يرجى إدخال رقم صحيح:", reply_markup=cancel_kb, parse_mode="Markdown")
            return
    except Exception:
        await message.answer("⚠️ إدخال غير صحيح. يرجى إدخال رقم لسعر الصرف:", reply_markup=cancel_kb, parse_mode="Markdown")
        return

    data = await state.get_data()
    amount_val = Decimal(data.get("counter_amount", data.get("current_amount", "10")))
    await _submit_maker_counter(message, state, amount_val, rate_val)


async def _submit_maker_counter(message: Message, state: FSMContext, amount_val: Decimal, rate_val: Decimal):
    data = await state.get_data()
    neg_id = data['neg_id']

    from db.session import AsyncSessionLocal
    async with AsyncSessionLocal() as db_session:
        result = await db_session.execute(
            select(Negotiation).where(Negotiation.id == neg_id).with_for_update()
        )
        neg = result.scalar_one_or_none()

        now = datetime.now(timezone.utc)
        from bot.utils.transitions import can_maker_counter_negotiation
        if not neg or not can_maker_counter_negotiation(neg.status, neg.expires_at, now):
            await db_session.commit()
            await message.answer("⚠️ جلسة التفاوض لم تعد صالحة للتعديل أو انتهت مهلتها.")
            await state.clear()
            return

        expires_at = now + timedelta(minutes=30)

        neg.proposed_amount = amount_val
        neg.proposed_rate = rate_val
        neg.status = "PENDING_TAKER"
        neg.round = 2
        neg.expires_at = expires_at

        maker_deals_res = await db_session.execute(
            select(Deal).where(
                or_(Deal.buyer_id == neg.maker_id, Deal.seller_id == neg.maker_id),
                Deal.status == "COMPLETED"
            )
        )
        maker_completed_count = len(maker_deals_res.scalars().all())
        await db_session.commit()

    await state.clear()

    amount_str = format_usdt(amount_val)
    rate_str = format_syp(rate_val)
    total_syp_str = format_syp(amount_val * rate_val)

    await message.answer(
        "📨 **تم إرسال العرض المضاد بنجاح**\n\n"
        f"▫️ **الكمية المقترحة:** `{amount_str} USDT`\n"
        f"▫️ **سعر الصرف النهائي:** `{rate_str}` ل.س\n"
        f"▫️ **الإجمالي:** `{total_syp_str}` ل.س\n"
        "▫️ **مهلة الرد:** 30 دقيقة\n\n"
        "بانتظار قرار الطرف المفاوض.",
        parse_mode="Markdown"
    )

    taker_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ قبول العرض النهائي", callback_data=f"neg_t_acc_{neg.id}"),
            InlineKeyboardButton(text="❌ رفض", callback_data=f"neg_t_rej_{neg.id}")
        ]
    ])

    taker_text = (
        f"🔄 **وصلك عرض مضاد — عرض #{neg.offer_id}**\n\n"
        f"▫️ **الكمية المقترحة:** `{amount_str} USDT`\n"
        f"▫️ **سعر الصرف:** `{rate_str}` ل.س\n"
        f"▫️ **الإجمالي:** `{total_syp_str}` ل.س\n"
        f"▫️ **سجل صاحب العرض:** `{maker_completed_count}` صفقة مكتملة\n"
        "▫️ **مهلة الرد:** 30 دقيقة\n\n"
        "هل توافق على هذا العرض النهائي لبدء تنفيذ الصفقة؟"
    )

    try:
        await message.bot.send_message(
            chat_id=neg.taker_id,
            text=taker_text,
            reply_markup=taker_kb,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify taker {neg.taker_id}: {e}")


# ================= 3. TAKER FINAL DECISION (ACCEPT / REJECT) =================

@negotiations_router.callback_query(F.data.startswith("neg_t_acc_"))
async def taker_accept_counter(callback: CallbackQuery, db_session: AsyncSession):
    neg_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(
        select(Negotiation).where(Negotiation.id == neg_id).with_for_update()
    )
    neg = result.scalar_one_or_none()

    if not neg:
        await callback.answer("عذراً، لم يتم العثور على جلسة التفاوض.", show_alert=True)
        return

    if callback.from_user.id != neg.taker_id and callback.from_user.id not in settings.ADMIN_IDS:
        await callback.answer("غير مصرح: هذا الإجراء للمفاوض فقط.", show_alert=True)
        return

    if neg.status != "PENDING_TAKER":
        await db_session.commit()
        await callback.answer("تم اتخاذ قرار مسبقاً بشأن هذا العرض.", show_alert=True)
        return

    success = await _finalize_negotiated_deal(callback.bot, neg, db_session, callback.message)
    if success:
        await callback.answer("✅ تم قبول العرض النهائي بنجاح!")
    else:
        await callback.answer()


@negotiations_router.callback_query(F.data.startswith("neg_t_rej_"))
async def taker_reject_counter(callback: CallbackQuery, db_session: AsyncSession):
    neg_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(
        select(Negotiation).where(Negotiation.id == neg_id).with_for_update()
    )
    neg = result.scalar_one_or_none()

    if not neg:
        await callback.answer("عذراً، لم يتم العثور على جلسة التفاوض.", show_alert=True)
        return

    if callback.from_user.id != neg.taker_id and callback.from_user.id not in settings.ADMIN_IDS:
        await callback.answer("غير مصرح: هذا الإجراء للمفاوض فقط.", show_alert=True)
        return

    from bot.utils.transitions import can_taker_reject_negotiation
    if not can_taker_reject_negotiation(neg.status):
        await db_session.commit()
        await callback.answer("تم اتخاذ قرار مسبقاً بشأن هذا العرض.", show_alert=True)
        return

    neg.status = "REJECTED"

    # Revert offer to active
    offer_id_val = neg.offer_id
    reverted_offer = None
    if offer_id_val:
        offer_res = await db_session.execute(
            select(Offer).where(Offer.id == offer_id_val).with_for_update()
        )
        offer = offer_res.scalar_one_or_none()
        if offer and offer.status == "negotiating":
            offer.status = "active"
            offer.status_changed_at = datetime.now(timezone.utc)
            reverted_offer = offer

    await db_session.commit()

    if reverted_offer:
        try:
            await update_channel_offer_message(callback.bot, reverted_offer)
        except Exception as e:
            logger.warning(f"Failed to update channel message for reverted offer #{offer_id_val}: {e}")

    await callback.answer("تم رفض العرض وإلغاء جلسة التفاوض.")
    await callback.message.edit_text(
        "❌ **تم إنهاء جلسة التفاوض**\n\n"
        f"تم رفض العرض المضاد وإعادة العرض #{neg.offer_id} إلى السوق كعرض نشط.",
        parse_mode="Markdown"
    )

    try:
        await callback.bot.send_message(
            chat_id=neg.maker_id,
            text=(
                f"ℹ️ **تحديث جلسة التفاوض — عرض #{neg.offer_id}**\n\n"
                "رفض الطرف المفاوض العرض المضاد.\n"
                "تمت إعادة عرضك إلى السوق كعرض نشط ومتاح."
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify maker {neg.maker_id}: {e}")


# ================= 4. CONVERT ACCEPTED NEGOTIATION TO DEAL =================

async def _finalize_negotiated_deal(bot: Bot, neg: Negotiation, db_session: AsyncSession, origin_message: Message) -> bool:
    # Lock the associated offer
    offer_res = await db_session.execute(
        select(Offer).where(Offer.id == neg.offer_id).with_for_update()
    )
    offer = offer_res.scalar_one_or_none()

    if not offer or offer.status != "negotiating":
        await db_session.commit()
        if isinstance(origin_message, Message):
            try:
                await origin_message.answer("⚠️ العرض المرتبط لم يعد متاحاً للتنفيذ حالياً.")
            except Exception:
                pass
        return False

    # Atomic state transition in a single transaction
    neg.status = "ACCEPTED"
    offer.status = "reserved"
    offer.status_changed_at = datetime.now(timezone.utc)

    # Determine seller and buyer
    if offer.offer_type == 'sell':
        seller_id = offer.user_id
        buyer_id = neg.taker_id
    else:
        seller_id = neg.taker_id
        buyer_id = offer.user_id

    if seller_id == buyer_id or neg.taker_id == offer.user_id:
        await db_session.rollback()
        if isinstance(origin_message, Message):
            try:
                await origin_message.answer("⚠️ عذراً، لا يمكن تنفيذ صفقة مع صاحب العرض نفسه.")
            except Exception:
                pass
        return False

    amount_usdt = neg.proposed_amount
    amount_syp = amount_usdt * neg.proposed_rate
    now = datetime.now(timezone.utc)
    deposit_expires_at = now + timedelta(minutes=15)

    from bot.handlers.deals import derive_escrow_address
    deposit_address, deposit_index = await derive_escrow_address(db_session)

    methods = list(offer.payment_methods) if offer.payment_methods else []
    payout_details = offer.payout_details if isinstance(offer.payout_details, dict) else {}
    need_buyer_pm_choice = False

    if offer.offer_type == 'buy':
        # Maker = Buyer (saved BEP-20 payout), Taker = Seller (provides SYP info after USDT deposit)
        buyer_payout = payout_details.get("usdt_bep20_address") or offer.payout_info or "PENDING"
        seller_info = (
            {"method": methods[0], "info": "PENDING"}
            if len(methods) == 1
            else {"method": "PENDING", "info": "PENDING"}
        )
    else:
        # Maker = Seller (has SYP accounts in payout_details), Taker = Buyer
        buyer_payout = "PENDING"
        if len(methods) == 1:
            selected_pm = methods[0]
            raw_acc = payout_details.get(selected_pm) or offer.payout_info or "PENDING"
            clean_acc = extract_clean_account(raw_acc, selected_pm) or "PENDING"
            seller_info = {"method": selected_pm, "info": clean_acc}
        else:
            seller_info = {"method": "PENDING", "info": "PENDING"}
            need_buyer_pm_choice = len(methods) > 1

    fee_breakdown = calculate_deal_fee(amount_usdt)

    new_deal = Deal(
        offer_id=offer.id,
        seller_id=seller_id,
        buyer_id=buyer_id,
        amount_usdt=amount_usdt,
        amount_syp=amount_syp,
        fee_usdt=fee_breakdown.total_fee,
        seller_fee_usdt=fee_breakdown.seller_fee,
        buyer_fee_usdt=fee_breakdown.buyer_fee,
        deposit_address=deposit_address,
        deposit_index=deposit_index,
        buyer_payout_address=buyer_payout,
        seller_payment_info=seller_info,
        status="AWAITING_USDT_DEPOSIT",
        expires_at=deposit_expires_at
    )
    try:
        db_session.add(new_deal)
        await db_session.commit()
        await db_session.refresh(new_deal)
    except IntegrityError:
        await db_session.rollback()
        if isinstance(origin_message, Message):
            try:
                await origin_message.answer("⚠️ عذراً، هذا العرض لم يعد متاحاً للتداول حالياً.")
            except Exception:
                pass
        return False

    # Post-commit channel update and user notifications (all wrapped in try/except)
    try:
        await update_channel_offer_message(bot, offer)
    except Exception as e:
        logger.warning(f"Failed to update channel message for deal #{new_deal.id} offer: {e}")

    # Dispatch unified Deal Card to both Counterparties
    try:
        await push_deal_card(new_deal.id, bot)
    except Exception as e:
        logger.warning(f"Failed to push deal card for negotiated deal #{new_deal.id}: {e}")

    # For multi-method Sell offers, let the Buyer choose their preferred SYP payment method
    if need_buyer_pm_choice:
        buttons = []
        for idx, m in enumerate(methods):
            icon = PAYMENT_METHOD_ICONS.get(m, "💳")
            buttons.append([InlineKeyboardButton(text=f"{icon} {m}", callback_data=f"nego_pm_{new_deal.id}_{idx}")])
        pm_kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        try:
            await bot.send_message(
                chat_id=buyer_id,
                text=(
                    f"💳 **اختيار وسيلة الدفع — صفقة #{new_deal.id}**\n\n"
                    f"تم الاتفاق على شروط التفاوض بنجاح.\n"
                    f"يدعم هذا العرض عدة وسائل دفع بالليرة السورية. يرجى اختيار الوسيلة التي ترغب بالتحويل عبرها:"
                ),
                reply_markup=pm_kb,
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.warning(f"Failed to prompt buyer {buyer_id} for payment method on deal #{new_deal.id}: {e}")

    return True


@negotiations_router.callback_query(F.data.startswith("nego_pm_"))
async def cq_nego_choose_payment_method(callback: CallbackQuery, db_session: AsyncSession):
    parts = callback.data.split("_")
    try:
        deal_id = int(parts[-2])
        idx = int(parts[-1])
    except (ValueError, IndexError):
        await callback.answer("⚠️ اختيار غير صالح.", show_alert=True)
        return

    res = await db_session.execute(select(Deal).where(Deal.id == deal_id).with_for_update())
    deal = res.scalar_one_or_none()
    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return

    if callback.from_user.id != deal.buyer_id and callback.from_user.id not in settings.ADMIN_IDS:
        await db_session.commit()
        await callback.answer("غير مصرح: اختيار وسيلة الدفع متاح للمشتري فقط.", show_alert=True)
        return

    if deal.status not in ("AWAITING_USDT_DEPOSIT", "CREATED", "USDT_DEPOSITED"):
        await db_session.commit()
        await callback.answer("لا يمكن تغيير وسيلة الدفع في هذه المرحلة من الصفقة.", show_alert=True)
        return

    existing_method = deal.seller_payment_info.get("method") if isinstance(deal.seller_payment_info, dict) else None
    if existing_method and existing_method not in ("غير محدد", "PENDING"):
        await db_session.commit()
        await callback.answer(f"تم تحديد وسيلة الدفع مسبقاً ({existing_method}).", show_alert=True)
        return

    offer_res = await db_session.execute(select(Offer).where(Offer.id == deal.offer_id))
    offer = offer_res.scalar_one_or_none()
    methods = list(offer.payment_methods) if (offer and offer.payment_methods) else []

    if idx < 0 or idx >= len(methods):
        await db_session.commit()
        await callback.answer("⚠️ وسيلة الدفع المحددة غير صالحة لهذا العرض.", show_alert=True)
        return

    chosen_pm = methods[idx]
    payout_details = offer.payout_details if (offer and isinstance(offer.payout_details, dict)) else {}
    raw_acc = payout_details.get(chosen_pm) or (offer.payout_info if offer else None) or "PENDING"
    clean_acc = extract_clean_account(raw_acc, chosen_pm) or "PENDING"

    deal.seller_payment_info = {"method": chosen_pm, "info": clean_acc}
    if deal.status == "USDT_DEPOSITED" and clean_acc != "PENDING":
        deal.status = "AWAITING_SYP_PAYMENT"
        deal.expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)

    await db_session.commit()

    try:
        await callback.message.edit_text(
            f"✅ **تم اعتماد وسيلة الدفع — صفقة #{deal.id}**\n\n"
            f"▫️ **الوسيلة المختارة:** {chosen_pm}",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    await callback.answer("تم اختيار وسيلة الدفع بنجاح ✅")
    try:
        await push_deal_card(deal.id, callback.bot)
    except Exception as e:
        logger.warning(f"Failed to push deal card after buyer method selection on deal #{deal.id}: {e}")
