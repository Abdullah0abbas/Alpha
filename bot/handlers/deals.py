from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import Union
import logging
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from aiogram.filters import Command
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_, func, text

from db.models import Deal, Offer, User
from bot.states.flow_states import DealCreateFSM, DealPaymentFSM
from bot.services.audit_service import record_audit, add_audit
from config.settings import settings
from aiogram import Bot
from bot.services.channel import update_channel_offer_message
from bot.utils.deal_views import format_admin_deal_review_text
from bot.utils.deal_guard import acquire_deal_lock
from bot.utils.validators import validate_bep20_address, validate_payment_method_info
from bot.utils.branding import BOT_NAME
from bot.utils.formatters import (
    escape_markdown,
    format_payment_methods,
    format_syp,
    format_usdt,
    humanize_time_remaining,
    clean_numeric_input,
    format_local_datetime
)
from bot.utils.transitions import (
    can_cancel_deal,
    can_seller_confirm_syp,
    can_admin_release_escrow,
    get_next_deal_status_after_syp_confirmed,
    SELLER_INFO_ALLOWED_STATUSES
)

logger = logging.getLogger(__name__)

deals_router = Router()

# ================= DEAL CREATION =================

@deals_router.callback_query(F.data.startswith("start_deal_"))
async def init_deal(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    # Check if user is banned
    user_res = await db_session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = user_res.scalar_one_or_none()
    if user and user.is_banned:
        await callback.answer(f"🚫 حسابك محظور من بدء أي صفقات جديدة على {BOT_NAME}.", show_alert=True)
        return

    # Section 6.1.1: Active deals cap check (max 5 active deals)
    user_id = callback.from_user.id
    if user_id not in settings.ADMIN_IDS:
        deals_count_res = await db_session.execute(
            select(func.count(Deal.id)).where(
                or_(Deal.seller_id == user_id, Deal.buyer_id == user_id),
                Deal.status.not_in(['COMPLETED', 'CANCELLED', 'EXPIRED'])
            )
        )
        active_deals_count = deals_count_res.scalar() or 0
        if active_deals_count >= 5:
            await callback.answer(
                "⚠️ لديك 5 صفقات نشطة بالفعل. يرجى إكمال أو إلغاء إحدى صفقاتك السابقة قبل بدء صفقة جديدة.",
                show_alert=True
            )
            return

    offer_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(
        select(Offer).where(Offer.id == offer_id).with_for_update()
    )
    offer = result.scalar_one_or_none()
    
    if not offer:
        await callback.answer("عذراً، لم يتم العثور على العرض.", show_alert=True)
        return
        
    if offer.status != "active":
        await callback.answer("عذراً، هذا العرض لم يعد متاحاً للتداول حالياً.", show_alert=True)
        return
        
    offer.status = "reserved"
    await db_session.commit()
    await update_channel_offer_message(callback.bot, offer)
        
    await state.update_data(
        offer_id=offer.id, 
        rate=str(offer.rate), 
        offer_type=offer.offer_type,
        creator_id=offer.user_id,
        payout_info=offer.payout_info,
        min_limit=str(offer.min_limit),
        max_limit=str(offer.max_limit)
    )
    
    offer_type_ar = "🔴 بيع USDT (المعلن يبيع)" if offer.offer_type == 'sell' else "🟢 شراء USDT (المعلن يشتري)"
    taker_role_ar = "🟢 مشتري USDT" if offer.offer_type == 'sell' else "🔴 بائع USDT"
    rate_str = format_syp(offer.rate)
    min_str = format_usdt(offer.min_limit)
    max_str = format_usdt(offer.max_limit)
    methods_str = format_payment_methods(offer.payment_methods)
    
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    
    await callback.message.answer(
        f"🤝 **بدء تنفيذ صفقة — عرض #{offer.id}**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📊 **بيانات العرض الأساسية:**\n"
        f"• النوع: {offer_type_ar}\n"
        f"• سعر الصرف: 💵 `{rate_str} SYP`\n"
        f"• حدود الكمية: 📦 `{min_str}` إلى `{max_str}` USDT\n"
        f"• وسائل الدفع المتاحة: 💳 {methods_str}\n"
        f"• دورك في المعاملة: **{taker_role_ar}**\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"⌨️ يرجى إدخال كمية USDT المطلوبة للتداول:",
        reply_markup=cancel_kb,
        parse_mode="Markdown"
    )
    await state.set_state(DealCreateFSM.waiting_for_amount)

@deals_router.message(DealCreateFSM.waiting_for_amount)
async def process_deal_amount(message: Message, state: FSMContext, db_session: AsyncSession):
    data = await state.get_data()
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    try:
        amount_usdt = clean_numeric_input(message.text)
        if not amount_usdt:
            raise ValueError("Invalid amount")

        min_limit = Decimal(data['min_limit'])
        max_limit = Decimal(data['max_limit'])
        
        min_str = format_usdt(min_limit)
        max_str = format_usdt(max_limit)

        if amount_usdt < min_limit or amount_usdt > max_limit:
            await message.answer(
                f"⚠️ **الكمية المدخلة خارج الحدود المسموحة!**\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"• الحد الأدنى: `{min_str} USDT`\n"
                f"• الحد الأقصى: `{max_str} USDT`\n"
                f"━━━━━━━━━━━━━━━\n\n"
                f"⌨️ يرجى إعادة إدخال كمية مناسبة ضمن هذا النطاق:",
                reply_markup=cancel_kb,
                parse_mode="Markdown"
            )
            return

        rate = Decimal(data['rate'])
        amount_syp = (amount_usdt * rate).quantize(Decimal("1"))
        await state.update_data(amount_usdt=str(amount_usdt), amount_syp=str(amount_syp))

        offer_type = data['offer_type']
        creator_id = data['creator_id']
        taker_id = message.from_user.id

        if offer_type == 'buy':
            # Maker is BUYER, Taker is SELLER
            seller_id = taker_id
            buyer_id = creator_id
            buyer_payout_address = data.get('payout_info') or "PENDING"
            seller_payment_info = {"info": "PENDING"}
        else:
            # Maker is SELLER, Taker is BUYER
            seller_id = creator_id
            buyer_id = taker_id
            buyer_payout_address = "PENDING"
            seller_payment_info = {"info": data.get('payout_info') or "PENDING"}

        await _finalize_deal_creation(
            message=message,
            state=state,
            db_session=db_session,
            data=data,
            seller_id=seller_id,
            buyer_id=buyer_id,
            buyer_payout_address=buyer_payout_address,
            seller_payment_info=seller_payment_info
        )
    except Exception:
        await message.answer(
            "⚠️ **قيمة غير صحيحة!**\n\n"
            "يرجى إدخال رقم صحيح وموجب (مثال: `100` أو `50.5`):",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
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
    seller_payment_info: dict
):
    offer_id = data['offer_id']
    amount_usdt = Decimal(data['amount_usdt'])
    amount_syp = Decimal(data['amount_syp'])
    creator_id = data['creator_id']
    taker_id = message.from_user.id

    mock_deposit_address, mock_deposit_index = await derive_escrow_address(db_session)

    now = datetime.now(timezone.utc)
    approval_expires_at = now + timedelta(minutes=30)

    new_deal = Deal(
        offer_id=offer_id,
        seller_id=seller_id,
        buyer_id=buyer_id,
        amount_usdt=amount_usdt,
        amount_syp=amount_syp,
        fee_usdt=Decimal("1.0"),
        deposit_address=mock_deposit_address,
        deposit_index=mock_deposit_index,
        buyer_payout_address=buyer_payout_address,
        seller_payment_info=seller_payment_info,
        status="PENDING_OWNER_APPROVAL",
        expires_at=approval_expires_at
    )
    db_session.add(new_deal)
    await db_session.commit()
    await db_session.refresh(new_deal)
    await state.clear()

    usdt_str = format_usdt(amount_usdt)
    syp_str = format_syp(amount_syp)

    # 1. Notify Taker (Requester)
    taker_role_ar = "🔴 بائع USDT" if taker_id == seller_id else "🟢 مشتري USDT"
    taker_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء الطلب", callback_data=f"ask_cancel_{new_deal.id}")]
    ])
    await message.answer(
        f"⏳ **طلب صفقة جديدة #{new_deal.id}**\n\n\n"
        f"• الحالة:  **بانتظار موافقة الطرف الآخر**\n\n"
        f"• دورك في الصفقة:  {taker_role_ar}\n\n"
        f"• الكمية:  `{usdt_str}` USDT\n\n"
        f"• المبلغ الإجمالي:  `{syp_str}` SYP\n\n"
        f"• المهلة المتاحة:  30 دقيقة\n\n\n"
        f"تم إرسال إشعار لصاحب العرض. سيتم إشعارك فور اتخاذ قراره للمتابعة.",
        reply_markup=taker_kb,
        parse_mode="Markdown"
    )

    # 2. Fetch Taker completed deals count from users table
    taker_user_res = await db_session.execute(select(User).where(User.telegram_id == taker_id))
    taker_user = taker_user_res.scalar_one_or_none()
    completed_deals = taker_user.completed_deals_count if taker_user else 0

    # 3. Notify Maker (Owner) - STRICT PRIVACY: only amount and completed deals count, NO personal info
    maker_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ موافقة", callback_data=f"appr_deal_{new_deal.id}"),
            InlineKeyboardButton(text="❌ رفض", callback_data=f"rejc_deal_{new_deal.id}")
        ]
    ])
    maker_role_ar = "🔴 بائع USDT" if creator_id == seller_id else "🟢 مشتري USDT"
    maker_text = (
        f"🔔 **طلب بدء صفقة جديد على عرضك #{offer_id}**\n\n\n"
        f"• رقم الصفقة:  `#{new_deal.id}`\n\n"
        f"• دورك في الصفقة:  {maker_role_ar}\n\n"
        f"• الكمية المطلوبة:  `{usdt_str}` USDT\n\n"
        f"• المبلغ الإجمالي:  `{syp_str}` SYP\n\n"
        f"• سجل الطرف الآخر:  `{completed_deals}` صفقة مكتملة\n\n"
        f"• المهلة المتاحة:  30 دقيقة\n\n\n"
        f"هل توافق على بدء تنفيذ الصفقة وحجز الضمان؟"
    )
    try:
        await message.bot.send_message(
            chat_id=creator_id,
            text=maker_text,
            reply_markup=maker_kb,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify offer creator {creator_id}: {e}")

# ================= OWNER APPROVAL / REJECTION HANDLERS =================

@deals_router.callback_query(F.data.startswith("appr_deal_"))
async def approve_deal_request(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(
        select(Deal).where(Deal.id == deal_id).with_for_update()
    )
    deal = result.scalar_one_or_none()

    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return

    # Check that caller is the maker
    offer_res = await db_session.execute(select(Offer).where(Offer.id == deal.offer_id))
    offer = offer_res.scalar_one_or_none()
    creator_id = offer.user_id if offer else None

    if callback.from_user.id != creator_id and callback.from_user.id not in settings.ADMIN_IDS:
        await callback.answer("غير مصرح: هذا الإجراء لصاحب العرض فقط.", show_alert=True)
        return

    if deal.status != "PENDING_OWNER_APPROVAL":
        await callback.answer("تم اتخاذ قرار مسبقاً بشأن هذا الطلب.", show_alert=True)
        return

    now = datetime.now(timezone.utc)
    deal.status = "CREATED"
    await db_session.flush()
    deal.status = "AWAITING_USDT_DEPOSIT"
    deal.expires_at = now + timedelta(minutes=15)
    await db_session.commit()

    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)

    await callback.answer("تمت الموافقة على الطلب بنجاح ✅")
    
    # Notify Seller (the one who must deposit USDT)
    seller_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأكيد إيداع الضمان", callback_data=f"man_dep_{deal.id}")]
    ])
    seller_msg_text = (
        f"🔐 **صفقة #{deal.id} — مرحلة إيداع رصيد الضمان**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• دورك: 🔴 بائع USDT\n"
        f"• القيمة المطلوبة: 💰 `{usdt_str} USDT` ⇄ `{syp_str} SYP`\n"
        f"• المهلة المتاحة للإيداع: ⏱️ 15 دقيقة\n\n"
        f"🔑 **عنوان محفظة الضمان للإيداع:**\n"
        f"`{deal.deposit_address}`\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"قم بتحويل `{usdt_str} USDT` إلى العنوان أعلاه، ثم اضغط على زر التأكيد أدناه:",
    )

    # Notify Buyer (waiting for seller to deposit)
    buyer_msg_text = (
        f"✅ **صفقة #{deal.id} — تمت موافقة البائع**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• دورك: 🟢 مشتري USDT\n"
        f"• القيمة المتفق عليها: 💰 `{usdt_str} USDT` ⇄ `{syp_str} SYP`\n"
        f"• المهلة الزمنية: ⏱️ 15 دقيقة\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"بدأت مهلة إيداع الضمان للبائع لحجز الرصيد بمحفظة الضمان الآمنة. سيصلك إشعار فور تأكيد حجز الرصيد."
    )

    seller_text_str = seller_msg_text[0] if isinstance(seller_msg_text, tuple) else seller_msg_text

    if callback.from_user.id == deal.seller_id:
        await callback.message.edit_text(seller_text_str, reply_markup=seller_kb, parse_mode="Markdown")
        try:
            await callback.bot.send_message(chat_id=deal.buyer_id, text=buyer_msg_text, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Failed to notify buyer {deal.buyer_id}: {e}")
    else:
        await callback.message.edit_text(buyer_msg_text, parse_mode="Markdown")
        try:
            await callback.bot.send_message(chat_id=deal.seller_id, text=seller_text_str, reply_markup=seller_kb, parse_mode="Markdown")
        except Exception as e:
            logger.warning(f"Failed to notify seller {deal.seller_id}: {e}")

@deals_router.callback_query(F.data.startswith("rejc_deal_"))
async def reject_deal_request(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(
        select(Deal).where(Deal.id == deal_id).with_for_update()
    )
    deal = result.scalar_one_or_none()

    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return

    offer_res = await db_session.execute(select(Offer).where(Offer.id == deal.offer_id))
    offer = offer_res.scalar_one_or_none()
    creator_id = offer.user_id if offer else None

    if callback.from_user.id != creator_id and callback.from_user.id not in settings.ADMIN_IDS:
        await callback.answer("غير مصرح: هذا الإجراء لصاحب العرض فقط.", show_alert=True)
        return

    if deal.status != "PENDING_OWNER_APPROVAL":
        await callback.answer("تم اتخاذ قرار مسبقاً بشأن هذا الطلب.", show_alert=True)
        return

    deal.status = "CANCELLED"
    if offer and offer.status == "reserved":
        offer.status = "active"

    await db_session.commit()
    if offer:
        await update_channel_offer_message(callback.bot, offer)

    await callback.answer("تم رفض الطلب.")
    await callback.message.edit_text(
        f"❌ **صفقة #{deal.id} — تم رفض الطلب**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"تم رفض طلب بدء الصفقة وإعادة العرض فوراً إلى السوق كعرض نشط ومتاح للجميع.",
        parse_mode="Markdown"
    )

    # Notify Taker (Requester)
    taker_id = deal.buyer_id if creator_id == deal.seller_id else deal.seller_id
    try:
        await callback.bot.send_message(
            chat_id=taker_id,
            text=(
                f"ℹ️ **تحديث صفقة #{deal.id}**\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"اعتذر صاحب العرض عن قبول طلب بدء الصفقة.\n"
                f"يمكنك استكشاف وتصفح باقي العروض المتاحة في السوق عبر /offers."
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify taker {taker_id} of rejection: {e}")

# ================= DEAL CANCELLATION (EARLY STATES ONLY) =================

@deals_router.callback_query(F.data.startswith("ask_cancel_"))
async def ask_cancel_deal(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()

    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return

    allowed, err_msg = can_cancel_deal(
        deal_status=deal.status,
        user_id=callback.from_user.id,
        seller_id=deal.seller_id,
        buyer_id=deal.buyer_id,
        admin_ids=settings.ADMIN_IDS
    )
    if not allowed:
        await callback.answer(err_msg, show_alert=True)
        return

    await db_session.commit()

    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)

    text = (
        f"⚠️ **تأكيد إلغاء الصفقة — صفقة #{deal.id}**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• الكمية:  `{usdt_str}` USDT\n"
        f"• الإجمالي:  `{syp_str}` SYP\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"سيتم إلغاء الصفقة وإعادة العرض إلى السوق.\n"
        f"تنبيه: لا يمكن التراجع عن هذا الإجراء بعد تنفيذه. هل ترغب بالتأكيد؟"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ نعم، إلغاء الصفقة", callback_data=f"cancel_deal_{deal.id}")],
        [InlineKeyboardButton(text="↩️ رجوع", callback_data=f"back_cancel_deal_{deal.id}")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@deals_router.callback_query(F.data.startswith("back_cancel_deal_"))
async def cq_back_cancel_deal(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()
    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return
    await db_session.commit()

    user_id = callback.from_user.id
    role = "🔴 بائع USDT" if deal.seller_id == user_id else "🟢 مشتري USDT"
    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)

    if deal.status == "PENDING_OWNER_APPROVAL":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء الطلب", callback_data=f"ask_cancel_{deal.id}")]
        ])
        text = (
            f"⏳ **طلب صفقة جديدة #{deal.id}**\n\n\n"
            f"• الحالة:  **بانتظار موافقة الطرف الآخر**\n\n"
            f"• دورك في الصفقة:  {role}\n\n"
            f"• الكمية:  `{usdt_str}` USDT\n\n"
            f"• المبلغ الإجمالي:  `{syp_str}` SYP\n\n"
            f"• المهلة المتاحة:  30 دقيقة\n\n\n"
            f"تم إرسال إشعار لصاحب العرض. سيتم إشعارك فور اتخاذ قراره للمتابعة."
        )
        try:
            await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    else:
        neutral_text = "تم الرجوع. يمكنك متابعة صفقتك من «صفقاتي النشطة»."
        neutral_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💼 صفقاتي النشطة", callback_data="main_my_deals")],
            [InlineKeyboardButton(text="🏠 الرئيسية", callback_data="menu_back_main")]
        ])
        try:
            await callback.message.edit_text(neutral_text, reply_markup=neutral_kb)
        except Exception:
            await callback.message.answer(neutral_text, reply_markup=neutral_kb)
    await callback.answer()


@deals_router.callback_query(F.data.startswith("cancel_deal_"))
async def cancel_deal(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(
        select(Deal).where(Deal.id == deal_id).with_for_update()
    )
    deal = result.scalar_one_or_none()

    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return

    allowed, err_msg = can_cancel_deal(
        deal_status=deal.status,
        user_id=callback.from_user.id,
        seller_id=deal.seller_id,
        buyer_id=deal.buyer_id,
        admin_ids=settings.ADMIN_IDS
    )
    if not allowed:
        await callback.answer(err_msg, show_alert=True)
        return

    previous_status = deal.status
    if callback.from_user.id in settings.ADMIN_IDS:
        actor_role = "admin"
    elif callback.from_user.id == deal.seller_id:
        actor_role = "seller"
    else:
        actor_role = "buyer"

    actor_id = callback.from_user.id

    deal.status = "CANCELLED"
    
    offer_result = await db_session.execute(select(Offer).where(Offer.id == deal.offer_id))
    offer = offer_result.scalar_one_or_none()
    if offer and offer.status == "reserved":
        offer.status = "active"

    add_audit(
        session=db_session,
        action="CANCEL_DEAL",
        target_type="deal",
        target_id=deal.id,
        actor_id=actor_id,
        actor_type="ADMIN" if actor_role == "admin" else "USER",
        details={
            "previous_status": previous_status,
            "actor_role": actor_role,
            "actor_id": actor_id
        }
    )
        
    await db_session.commit()
    
    if offer:
        try:
            await update_channel_offer_message(callback.bot, offer)
        except Exception as e:
            logger.warning(f"Failed to update channel offer message for cancelled deal #{deal.id}: {e}")
        
    try:
        await callback.message.edit_text(
            f"❌ **صفقة #{deal.id} — تم إلغاء الصفقة**\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"تم إلغاء الصفقة بنجاح وإعادة العرض إلى السوق.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to edit cancel message for deal #{deal.id}: {e}")
    try:
        await callback.answer()
    except Exception:
        pass

# ================= UNIFIED PAYMENT & WALLET HELPERS =================

async def apply_seller_payment_info(
    deal: Deal,
    info_text: str,
    bot: Bot,
    db_session: AsyncSession,
    event: Union[CallbackQuery, Message]
) -> None:
    if deal.status != "USDT_DEPOSITED":
        raise ValueError(f"Invalid deal status for seller payment info: {deal.status}")

    # TODO: بند منفصل — تحقق صيغة عنوان BEP20 (0x + 40 hex) وEIP-55 checksum قبل الحفظ.
    deal.seller_payment_info = {"info": info_text}
    deal.status = "AWAITING_SYP_PAYMENT"
    await db_session.commit()

    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)
    escaped_info = escape_markdown(info_text)

    seller_text = (
        f"✅ **صفقة #{deal.id} — تم اعتماد حساب الاستلام بنجاح**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• حسابك المعتمد: 💳 `{escaped_info}`\n"
        f"• المبلغ المطلوب: 💰 `{syp_str} SYP`\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"تم إرسال بيانات الحساب للمشتري لتحويل المبلغ.\n"
        f"سيصلك إشعار فور تأكيد المشتري إتمام التحويل."
    )

    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(seller_text, parse_mode="Markdown")
        except Exception:
            await event.message.answer(seller_text, parse_mode="Markdown")
        await event.answer()
    else:
        await event.answer(seller_text, parse_mode="Markdown")

    # Send payment instructions and Action button directly to the BUYER
    buyer_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأكيد تحويل الليرة", callback_data=f"man_syp_{deal.id}")]
    ])
    try:
        await bot.send_message(
            chat_id=deal.buyer_id,
            text=(
                f"💸 **صفقة #{deal.id} — مطلوب تحويل الدفعة المحلية (SYP)**\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"• دورك: 🟢 مشتري USDT\n"
                f"• المبلغ المطلوب تحويله: 💰 `{syp_str} SYP`\n"
                f"• رصيد الضمان المحجوز: 🔒 `{usdt_str} USDT`\n\n"
                f"💳 **بيانات حساب استلام البائع:**\n"
                f"`{escaped_info}`\n"
                f"━━━━━━━━━━━━━━━\n\n"
                f"قم بتحويل المبلغ بدقة إلى حساب البائع أعلاه، ثم اضغط على زر التأكيد أدناه:"
            ),
            reply_markup=buyer_kb,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify buyer {deal.buyer_id} of payment info: {e}")


async def apply_buyer_payout_wallet(
    deal: Deal,
    wallet_address: str,
    bot: Bot,
    db_session: AsyncSession,
    event: Union[CallbackQuery, Message]
) -> None:
    if deal.status not in ("AWAITING_SYP_PAYMENT", "ADMIN_APPROVAL"):
        raise ValueError(f"Invalid deal status for buyer payout wallet: {deal.status}")

    is_valid, normalized_address, err_msg = validate_bep20_address(wallet_address)
    if not is_valid:
        raise ValueError(f"Invalid BEP-20 wallet address: {err_msg}")

    deal.buyer_payout_address = normalized_address

    if deal.status == "AWAITING_SYP_PAYMENT":
        deal.status = "SYP_PAYMENT_SENT"
        await db_session.commit()

        usdt_str = format_usdt(deal.amount_usdt)
        syp_str = format_syp(deal.amount_syp)
        escaped_wallet = escape_markdown(normalized_address)

        buyer_text = (
            f"✅ **صفقة #{deal.id} — تم اعتماد المحفظة وتأكيد التحويل بنجاح**\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"• القيمة: 💰 `{usdt_str} USDT` ⇄ `{syp_str} SYP`\n"
            f"• محفظة الاستلام المعتمدة (BEP-20):\n`{escaped_wallet}`\n"
            f"━━━━━━━━━━━━━━━\n\n"
            f"تم إشعار البائع للتحقق ومطابقة الحساب وتأكيد استلام الليرة."
        )

        if isinstance(event, CallbackQuery):
            try:
                await event.message.edit_text(buyer_text, parse_mode="Markdown")
            except Exception:
                await event.message.answer(buyer_text, parse_mode="Markdown")
            await event.answer()
        else:
            await event.answer(buyer_text, parse_mode="Markdown")

        # Send prompt with Confirmation button to the SELLER
        seller_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ تأكيد استلام الليرة", callback_data=f"ask_conf_{deal.id}")]
        ])
        try:
            await bot.send_message(
                chat_id=deal.seller_id,
                text=(
                    f"🔔 **صفقة #{deal.id} — إشعار تحويل دفعة من المشتري**\n\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"• دورك: 🔴 بائع USDT\n"
                    f"• المبلغ المحول لحسابك: 💰 `{syp_str} SYP`\n"
                    f"━━━━━━━━━━━━━━━\n\n"
                    f"يرجى فتح تطبيق حسابك والتأكد من وصول المبلغ كاملاً، ثم اضغط على زر التأكيد أدناه:"
                ),
                reply_markup=seller_kb,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Failed to notify seller {deal.seller_id}: {e}")

    elif deal.status == "ADMIN_APPROVAL":
        await db_session.commit()

        escaped_wallet = escape_markdown(normalized_address)
        buyer_text = (
            f"✅ **صفقة #{deal.id} — تم تسجيل عنوان محفظة الاستلام بنجاح**\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"• محفظة الاستلام المعتمدة (BEP-20):\n`{escaped_wallet}`\n"
            f"━━━━━━━━━━━━━━━\n\n"
            f"الصفقة حالياً قيد المراجعة والاعتماد الإداري لإطلاق الرصيد."
        )

        if isinstance(event, CallbackQuery):
            try:
                await event.message.edit_text(buyer_text, parse_mode="Markdown")
            except Exception:
                await event.message.answer(buyer_text, parse_mode="Markdown")
            await event.answer()
        else:
            await event.answer(buyer_text, parse_mode="Markdown")

        # Notify Admins that payout address has been registered
        admin_notice = (
            f"ℹ️ **تحديث صفقة #{deal.id}**\n\n"
            f"قام المشتري بتسجيل عنوان محفظة الاستلام (BEP-20):\n"
            f"`{escaped_wallet}`\n\n"
            f"الصفقة بانتظار المراجعة والاعتماد الإداري."
        )
        for admin_id in settings.ADMIN_IDS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=admin_notice,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.warning(f"Failed to notify admin {admin_id} for deal #{deal.id}: {e}")


# ================= MANUAL STATE TRANSITIONS & PAYMENT FLOW =================

@deals_router.callback_query(F.data.startswith("man_aw_dep_"))
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
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأكيد إيداع الضمان", callback_data=f"man_dep_{deal.id}")]
    ])
    escaped_deposit = escape_markdown(deal.deposit_address)
    await callback.message.edit_text(
        f"🔐 **صفقة #{deal.id} — بانتظار إيداع الضمان**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• دورك: 🔴 بائع USDT\n"
        f"• القيمة المطلوبة: 💰 `{usdt_str} USDT` ⇄ `{syp_str} SYP`\n"
        f"• المهلة المتاحة للإيداع: ⏱️ 15 دقيقة\n\n"
        f"🔑 **عنوان محفظة الضمان للإيداع:**\n"
        f"`{escaped_deposit}`\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"قم بتحويل `{usdt_str} USDT` ثم اضغط على زر تأكيد الإيداع:",
        reply_markup=kb,
        parse_mode="Markdown"
    )

    # Notify Buyer
    try:
        await callback.bot.send_message(
            chat_id=deal.buyer_id,
            text=(
                f"ℹ️ **تحديث صفقة #{deal.id}**\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"• دورك: 🟢 مشتري USDT\n"
                f"• القيمة: 💰 `{usdt_str} USDT` ⇄ `{syp_str} SYP`\n\n"
                f"بدأ البائع مهلة إيداع الضمان (15 دقيقة). سيتم إشعارك فور تأكيد استلام الرصيد."
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify buyer {deal.buyer_id}: {e}")


@deals_router.callback_query(F.data.startswith("man_dep_"))
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
    await db_session.commit()
    
    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)

    # Notify Buyer
    try:
        await callback.bot.send_message(
            chat_id=deal.buyer_id,
            text=(
                f"🔒 **تحديث صفقة #{deal.id} — تم تأمين وحجز الضمان**\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"• دورك: 🟢 مشتري USDT\n"
                f"• رصيد الضمان المحجوز: 💰 `{usdt_str} USDT` ⇄ `{syp_str} SYP`\n"
                f"━━━━━━━━━━━━━━━\n\n"
                f"تم تأكيد إيداع وحجز رصيد الضمان في محفظة ضمان {BOT_NAME}.\n"
                f"يقوم البائع الآن بإدخال بيانات حسابه المالي لتتمكن من التحويل له."
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify buyer {deal.buyer_id}: {e}")

    # Section 16 (item 2.1): Check if seller has saved payment info in profile
    user_res = await db_session.execute(select(User).where(User.telegram_id == deal.seller_id))
    seller_user = user_res.scalar_one_or_none()
    saved_info = seller_user.saved_payment_info.strip() if (seller_user and seller_user.saved_payment_info) else None

    if saved_info:
        # Show explicit Confirmation Gate
        gate_text = (
            f"🔒 **صفقة #{deal.id} — تم تأكيد وحجز الضمان بنجاح**\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"• رصيد الضمان المحجوز: 💰 `{usdt_str} USDT` ⇄ `{syp_str} SYP`\n"
            f"• حساب استلام الليرة المحفوظ بملفك الشخصي:\n"
            f"`{escape_markdown(saved_info)}`\n"
            f"━━━━━━━━━━━━━━━\n\n"
            f"هل ترغب في استخدام هذه البيانات المحفوظة، أم إدخال بيانات جديدة خاصة بهذه الصفقة؟"
        )
        gate_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ استخدام هذه البيانات", callback_data=f"use_saved_seller_info_{deal.id}")],
            [InlineKeyboardButton(text="✏️ إدخال بيانات جديدة لهذه الصفقة", callback_data=f"manual_set_seller_info_{deal.id}")],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
        ])
        await callback.message.edit_text(gate_text, reply_markup=gate_kb, parse_mode="Markdown")
        await callback.answer()
        return

    # Show payment method selection keyboard for seller
    await callback.message.edit_text(
        f"🔒 **صفقة #{deal.id} — تم تأكيد وحجز الضمان بنجاح**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• دورك: 🔴 بائع USDT\n"
        f"• رصيد الضمان المحجوز: 💰 `{usdt_str} USDT` ⇄ `{syp_str} SYP`\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"اختر وسيلة الدفع التي ترغب باستلام الليرة السورية عبرها:",
        reply_markup=get_deal_payment_methods_kb(deal.id),
        parse_mode="Markdown"
    )
    await callback.answer()


def get_deal_payment_methods_kb(deal_id: int) -> InlineKeyboardMarkup:
    """Builds clean, deduplicated payment method buttons for a deal."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💳 شام كاش ليرة", callback_data=f"deal_pm_sham_lira_{deal_id}"),
            InlineKeyboardButton(text="💵 شام كاش دولار", callback_data=f"deal_pm_sham_usd_{deal_id}")
        ],
        [
            InlineKeyboardButton(text="📱 سيريتيل كاش", callback_data=f"deal_pm_syriatel_{deal_id}")
        ],
        [
            InlineKeyboardButton(text="🏛️ حوالة بنكية (الهرم)", callback_data=f"deal_pm_haram_{deal_id}"),
            InlineKeyboardButton(text="🏢 حوالة بنكية (الفؤاد)", callback_data=f"deal_pm_fouad_{deal_id}")
        ],
        [
            InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")
        ]
    ])


DEAL_PM_PROMPTS = {
    "sham_lira": {
        "name": "شام كاش ليرة",
        "prompt": (
            "💳 **صفقة #{deal_id} — إدخال حساب: شام كاش ليرة**\n\n"
            "━━━━━━━━━━━━━━━\n"
            "يرجى إرسال عنوان حساب شام كاش الخاص بك (مكوّن من 32 خانة هكسا).\n\n"
            "📋 **مثال صالح:**\n"
            "`82b2a4fafc335c4f39c58910248c63d1`"
        )
    },
    "sham_usd": {
        "name": "شام كاش دولار",
        "prompt": (
            "💵 **صفقة #{deal_id} — إدخال حساب: شام كاش دولار**\n\n"
            "━━━━━━━━━━━━━━━\n"
            "يرجى إرسال عنوان حساب شام كاش الخاص بك (مكوّن من 32 خانة هكسا).\n\n"
            "📋 **مثال صالح:**\n"
            "`82b2a4fafc335c4f39c58910248c63d1`"
        )
    },
    "syriatel": {
        "name": "سيريتيل كاش",
        "prompt": (
            "📱 **صفقة #{deal_id} — إدخال حساب: سيريتيل كاش**\n\n"
            "━━━━━━━━━━━━━━━\n"
            "يرجى إرسال رقم سيريتيل كاش الخاص بك (رقم سوري مكوّن من 10 أرقام يبدأ بـ 09).\n\n"
            "📋 **مثال صالح:**\n"
            "`0932063758`"
        )
    },
    "haram": {
        "name": "حوالة بنكية (الهرم)",
        "prompt": (
            "🏛️ **صفقة #{deal_id} — إدخال بيانات: حوالة بنكية (الهرم)**\n\n"
            "━━━━━━━━━━━━━━━\n"
            "يرجى إرسال الاسم الثلاثي ورقم الموبايل الخاص بك لاستلام الحوالة.\n\n"
            "📋 **مثال صالح:**\n"
            "`محمد أحمد العلي - 0932063758`"
        )
    },
    "fouad": {
        "name": "حوالة بنكية (الفؤاد)",
        "prompt": (
            "🏢 **صفقة #{deal_id} — إدخال بيانات: حوالة بنكية (الفؤاد)**\n\n"
            "━━━━━━━━━━━━━━━\n"
            "يرجى إرسال الاسم الثلاثي ورقم الموبايل الخاص بك لاستلام الحوالة.\n\n"
            "📋 **مثال صالح:**\n"
            "`محمد أحمد العلي - 0932063758`"
        )
    },
}


@deals_router.callback_query(F.data.startswith("deal_pm_"))
async def cq_deal_choose_payment_method(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    parts = callback.data.split("_")
    deal_id = int(parts[-1])
    method_key = "_".join(parts[2:-1])

    pm_info = DEAL_PM_PROMPTS.get(method_key)
    if not pm_info:
        await callback.answer("طريقة دفع غير معروفة.", show_alert=True)
        return

    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses=SELLER_INFO_ALLOWED_STATUSES,
        allowed_roles="seller",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    await db_session.commit()

    await state.set_state(DealPaymentFSM.waiting_for_seller_account)
    await state.update_data(deal_id=deal_id, selected_pm=pm_info["name"])

    prompt_text = pm_info["prompt"].format(deal_id=deal_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    try:
        sent_msg = await callback.message.edit_text(prompt_text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    except Exception:
        sent_msg = await callback.message.answer(prompt_text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    await callback.answer()


@deals_router.callback_query(F.data.startswith("set_seller_info_"))
async def start_set_seller_info(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses=SELLER_INFO_ALLOWED_STATUSES,
        allowed_roles="seller",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    # Section 16 (item 2.1): Check if seller has saved payment info in profile
    user_res = await db_session.execute(select(User).where(User.telegram_id == deal.seller_id))
    seller_user = user_res.scalar_one_or_none()
    saved_info = seller_user.saved_payment_info.strip() if (seller_user and seller_user.saved_payment_info) else None

    # Release row lock before Telegram calls without expiring attributes
    await db_session.commit()

    if saved_info:
        # Show explicit Confirmation Gate
        gate_text = (
            f"💳 **صفقة #{deal.id} — حساب استلام الليرة (SYP)**\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"• البيانات المحفوظة بملفك الشخصي:\n"
            f"`{escape_markdown(saved_info)}`\n"
            f"━━━━━━━━━━━━━━━\n\n"
            f"هل ترغب في استخدام هذه البيانات المحفوظة، أم إدخال بيانات جديدة خاصة بهذه الصفقة؟"
        )
        gate_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ استخدام هذه البيانات", callback_data=f"use_saved_seller_info_{deal.id}")],
            [InlineKeyboardButton(text="✏️ إدخال بيانات جديدة لهذه الصفقة", callback_data=f"manual_set_seller_info_{deal.id}")],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
        ])
        try:
            await callback.message.edit_text(gate_text, reply_markup=gate_kb, parse_mode="Markdown")
        except Exception:
            await callback.message.answer(gate_text, reply_markup=gate_kb, parse_mode="Markdown")
        await callback.answer()
        return

    # Fallback to method selection if no saved info
    curr_info = deal.seller_payment_info.get("info", "") if isinstance(deal.seller_payment_info, dict) else ""
    curr_info_str = f"\n• البيانات المسجلة حالياً: `{escape_markdown(curr_info)}`" if curr_info and curr_info not in ["PENDING", "غير محدد", "N/A"] else ""

    await callback.message.answer(
        f"💳 **صفقة #{deal.id} — حساب استلام الليرة (SYP)**\n\n"
        f"━━━━━━━━━━━━━━━{curr_info_str}\n"
        f"اختر وسيلة الدفع التي ترغب باستلام الدفعة عبرها:",
        reply_markup=get_deal_payment_methods_kb(deal.id),
        parse_mode="Markdown"
    )
    await callback.answer()


@deals_router.callback_query(F.data.startswith("use_saved_seller_info_"))
async def cq_use_saved_seller_info(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses=SELLER_INFO_ALLOWED_STATUSES,
        allowed_roles="seller",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    user_res = await db_session.execute(select(User).where(User.telegram_id == deal.seller_id))
    seller_user = user_res.scalar_one_or_none()
    saved_info = seller_user.saved_payment_info.strip() if (seller_user and seller_user.saved_payment_info) else None

    if not saved_info:
        await callback.answer("لم يتم العثور على بيانات محفوظة.", show_alert=True)
        return

    await apply_seller_payment_info(
        deal=deal,
        info_text=saved_info,
        bot=callback.bot,
        db_session=db_session,
        event=callback
    )


@deals_router.callback_query(F.data.startswith("manual_set_seller_info_"))
async def cq_manual_set_seller_info(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses=SELLER_INFO_ALLOWED_STATUSES,
        allowed_roles="seller",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    # Release row lock before Telegram calls without expiring attributes
    await db_session.commit()

    text = (
        f"💳 **صفقة #{deal.id} — اختيار وسيلة استلام الليرة (SYP)**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"اختر وسيلة الدفع التي ترغب باعتمادها لهذه الصفقة:"
    )
    kb = get_deal_payment_methods_kb(deal.id)
    try:
        sent_msg = await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    except Exception:
        sent_msg = await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    await callback.answer()


@deals_router.message(DealPaymentFSM.waiting_for_seller_account)
async def process_set_seller_info(message: Message, state: FSMContext, db_session: AsyncSession):
    info_text = message.text.strip() if message.text else ""
    if not info_text:
        await message.answer("يرجى إدخال بيانات صحيحة وغير فارغة:")
        return

    data = await state.get_data()
    deal_id = data.get('deal_id')
    selected_pm = data.get('selected_pm')
    if not deal_id:
        await message.answer("حدث خطأ في استعادة بيانات الجلسة.")
        await state.clear()
        return

    is_valid, formatted_info, err_msg = validate_payment_method_info(selected_pm, info_text)
    if not is_valid:
        warn_msg = await message.answer(
            f"{err_msg}\n\nيرجى إعادة إدخال البيانات بالشكل المطلوب أو الضغط على إلغاء:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
            ]),
            parse_mode="Markdown"
        )
        await state.update_data(prompt_msg_id=warn_msg.message_id, prompt_chat_id=warn_msg.chat.id)
        return

    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=message.from_user.id,
        expected_statuses=SELLER_INFO_ALLOWED_STATUSES,
        allowed_roles="seller",
        db_session=db_session
    )
    if not valid:
        await db_session.commit()
        await message.answer(err_msg)
        await state.clear()
        return

    await state.clear()
    await apply_seller_payment_info(
        deal=deal,
        info_text=formatted_info,
        bot=message.bot,
        db_session=db_session,
        event=message
    )


@deals_router.callback_query(F.data.startswith("man_aw_syp_"))
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

    seller_info_val = deal.seller_payment_info.get("info") if isinstance(deal.seller_payment_info, dict) else ""
    if not seller_info_val or seller_info_val in ["PENDING", "غير محدد", "N/A"]:
        # Seamlessly route seller to set payment info to eliminate dead-ends
        await start_set_seller_info(callback, state, db_session)
        return

    deal.status = "AWAITING_SYP_PAYMENT"
    await db_session.commit()
    
    payment_info_str = escape_markdown(seller_info_val)
    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)

    # 1. Update Seller's UI
    await callback.message.edit_text(
        f"⏳ **صفقة #{deal.id} — بانتظار تحويل الدفعة المحلية (SYP)**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• دورك: 🔴 بائع USDT\n"
        f"• المبلغ المتوقع: 💰 `{usdt_str} USDT` ⇄ `{syp_str} SYP`\n"
        f"• حساب الاستلام المعتمد: 💳 `{payment_info_str}`\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"تم إشعار المشتري بالتحويل. سيصلك إشعار فور تأكيد الدفع من قِبله.",
        parse_mode="Markdown"
    )

    # 2. Send prompt with Action button directly to the BUYER
    buyer_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأكيد تحويل الليرة", callback_data=f"man_syp_{deal.id}")]
    ])
    try:
        await callback.bot.send_message(
            chat_id=deal.buyer_id,
            text=(
                f"💸 **صفقة #{deal.id} — مطلوب تحويل الدفعة المحلية (SYP)**\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"• دورك: 🟢 مشتري USDT\n"
                f"• المبلغ المطلوب تحويله: 💰 `{syp_str} SYP`\n"
                f"• رصيد الضمان المحجوز: 🔒 `{usdt_str} USDT`\n\n"
                f"💳 **حساب استلام البائع:**\n"
                f"`{payment_info_str}`\n"
                f"━━━━━━━━━━━━━━━\n\n"
                f"قم بتحويل المبلغ للبائع ثم اضغط على زر التأكيد أدناه:",
            ),
            reply_markup=buyer_kb,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify buyer {deal.buyer_id}: {e}")


@deals_router.callback_query(F.data.startswith("man_syp_"))
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

    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)

    # Section 16 (item 2.1): Check if buyer has saved BEP-20 wallet address in profile
    user_res = await db_session.execute(select(User).where(User.telegram_id == deal.buyer_id))
    buyer_user = user_res.scalar_one_or_none()
    saved_wallet = buyer_user.saved_usdt_address.strip() if (buyer_user and buyer_user.saved_usdt_address) else None

    is_saved_valid, normalized_saved, _ = validate_bep20_address(saved_wallet) if saved_wallet else (False, "", "")

    # Release row lock before Telegram calls without expiring attributes
    await db_session.commit()

    if is_saved_valid:
        # Show explicit Confirmation Gate
        gate_text = (
            f"💸 **صفقة #{deal.id} — تأكيد تحويل الليرة وعنوان محفظة USDT**\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"• القيمة: 💰 `{usdt_str} USDT` ⇄ `{syp_str} SYP`\n"
            f"• عنوان المحفظة المحفوظ بملفك الشخصي (BEP-20):\n"
            f"`{escape_markdown(normalized_saved)}`\n"
            f"━━━━━━━━━━━━━━━\n\n"
            f"⚠️ **تنبيه:** تأكد أن هذا العنوان يدعم شبكة BNB Smart Chain (BEP20).\n\n"
            f"هل ترغب في استخدام هذا العنوان المحفوظ لتلقي الرصيد، أم إدخال عنوان محفظة جديد لهذه الصفقة؟"
        )
        gate_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ استخدام هذه البيانات", callback_data=f"use_saved_buyer_wallet_{deal.id}")],
            [InlineKeyboardButton(text="✏️ إدخال بيانات جديدة لهذه الصفقة", callback_data=f"manual_set_buyer_wallet_{deal.id}")],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
        ])
        await callback.message.edit_text(gate_text, reply_markup=gate_kb, parse_mode="Markdown")
        await callback.answer()
        return

    # Fallback to direct input via FSM if no saved wallet
    await state.set_state(DealPaymentFSM.waiting_for_buyer_wallet)
    await state.update_data(deal_id=deal.id)

    await callback.message.edit_text(
        f"📥 **صفقة #{deal.id} — محفظة استلام USDT**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"تم تسجيل تحويل الليرة بنجاح.\n\n"
        f"أدخل عنوان محفظتك الخاصة (BEP-20 / BNB Smart Chain) لتلقي رصيد `{usdt_str} USDT` عليها فور اعتماد الإدارة:\n"
        f"━━━━━━━━━━━━━━━",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
        ]),
        parse_mode="Markdown"
    )
    await callback.answer()


@deals_router.callback_query(F.data.startswith("set_buyer_payout_"))
async def start_set_buyer_payout(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses=["AWAITING_SYP_PAYMENT", "ADMIN_APPROVAL"],
        allowed_roles="buyer",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    if deal.status == "ADMIN_APPROVAL":
        curr_addr = (deal.buyer_payout_address or "").strip()
        if curr_addr and curr_addr not in ["PENDING", "غير محدد", "N/A"]:
            await db_session.commit()
            await callback.answer(
                "عنوان المحفظة مسجل مسبقاً ولا يمكن تغييره بعد وصول الصفقة للمراجعة الإدارية.",
                show_alert=True
            )
            return

    # Section 16 (item 2.1): Check if buyer has saved BEP-20 wallet address in profile
    user_res = await db_session.execute(select(User).where(User.telegram_id == deal.buyer_id))
    buyer_user = user_res.scalar_one_or_none()
    saved_wallet = buyer_user.saved_usdt_address.strip() if (buyer_user and buyer_user.saved_usdt_address) else None

    is_saved_valid, normalized_saved, _ = validate_bep20_address(saved_wallet) if saved_wallet else (False, "", "")

    # Release row lock before Telegram calls without expiring attributes
    await db_session.commit()

    if is_saved_valid:
        # Show explicit Confirmation Gate
        gate_text = (
            f"📥 **صفقة #{deal.id} — عنوان محفظة استلام USDT (BEP-20)**\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"• العنوان المحفوظ بملفك الشخصي:\n"
            f"`{escape_markdown(normalized_saved)}`\n"
            f"━━━━━━━━━━━━━━━\n\n"
            f"⚠️ **تنبيه:** تأكد أن هذا العنوان يدعم شبكة BNB Smart Chain (BEP20).\n\n"
            f"هل ترغب في استخدام هذا العنوان المحفوظ، أم إدخال عنوان محفظة جديد لهذه الصفقة؟"
        )
        gate_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ استخدام هذه البيانات", callback_data=f"use_saved_buyer_wallet_{deal.id}")],
            [InlineKeyboardButton(text="✏️ إدخال بيانات جديدة لهذه الصفقة", callback_data=f"manual_set_buyer_wallet_{deal.id}")],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
        ])
        try:
            await callback.message.edit_text(gate_text, reply_markup=gate_kb, parse_mode="Markdown")
        except Exception:
            await callback.message.answer(gate_text, reply_markup=gate_kb, parse_mode="Markdown")
        await callback.answer()
        return

    # Fallback to direct input if no saved wallet
    await state.set_state(DealPaymentFSM.waiting_for_buyer_wallet)
    await state.update_data(deal_id=deal_id)

    curr_addr = deal.buyer_payout_address or ""
    curr_addr_str = f"\n• العنوان المسجل حالياً: `{escape_markdown(curr_addr)}`" if curr_addr and curr_addr not in ["PENDING", "غير محدد", "N/A"] else ""

    await callback.message.answer(
        f"📥 **صفقة #{deal.id} — عنوان محفظة استلام USDT**\n\n"
        f"━━━━━━━━━━━━━━━{curr_addr_str}\n"
        f"أدخل عنوان محفظتك الخاصة المشفرة (BEP-20 / BNB Smart Chain) لتلقي الرصيد عليها فور اعتماد الإدارة:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
        ]),
        parse_mode="Markdown"
    )
    await callback.answer()


@deals_router.callback_query(F.data.startswith("use_saved_buyer_wallet_"))
async def cq_use_saved_buyer_wallet(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses=["AWAITING_SYP_PAYMENT", "ADMIN_APPROVAL"],
        allowed_roles="buyer",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    if deal.status == "ADMIN_APPROVAL":
        curr_addr = (deal.buyer_payout_address or "").strip()
        if curr_addr and curr_addr not in ["PENDING", "غير محدد", "N/A"]:
            await callback.answer(
                "عنوان المحفظة مسجل مسبقاً ولا يمكن تغييره بعد وصول الصفقة للمراجعة الإدارية.",
                show_alert=True
            )
            return

    user_res = await db_session.execute(select(User).where(User.telegram_id == deal.buyer_id))
    buyer_user = user_res.scalar_one_or_none()
    saved_wallet = buyer_user.saved_usdt_address.strip() if (buyer_user and buyer_user.saved_usdt_address) else None

    if not saved_wallet:
        await callback.answer("لم يتم العثور على عنوان محفوظ.", show_alert=True)
        return

    is_valid, normalized_wallet, err_msg = validate_bep20_address(saved_wallet)
    if not is_valid:
        await callback.answer(f"⚠️ عنوان المحفظة المحفوظ غير صالح: {err_msg}", show_alert=True)
        return

    await apply_buyer_payout_wallet(
        deal=deal,
        wallet_address=normalized_wallet,
        bot=callback.bot,
        db_session=db_session,
        event=callback
    )


@deals_router.callback_query(F.data.startswith("manual_set_buyer_wallet_"))
async def cq_manual_set_buyer_wallet(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses=["AWAITING_SYP_PAYMENT", "ADMIN_APPROVAL"],
        allowed_roles="buyer",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    if deal.status == "ADMIN_APPROVAL":
        curr_addr = (deal.buyer_payout_address or "").strip()
        if curr_addr and curr_addr not in ["PENDING", "غير محدد", "N/A"]:
            await db_session.commit()
            await callback.answer(
                "عنوان المحفظة مسجل مسبقاً ولا يمكن تغييره بعد وصول الصفقة للمراجعة الإدارية.",
                show_alert=True
            )
            return

    await state.set_state(DealPaymentFSM.waiting_for_buyer_wallet)
    await state.update_data(deal_id=deal_id)

    # Release row lock before Telegram calls without expiring attributes
    await db_session.commit()

    text = (
        f"📥 **صفقة #{deal.id} — إدخال عنوان محفظة جديد (BEP-20 / BSC)**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"أدخل عنوان محفظتك الخاصة المشفرة (BEP-20 / BNB Smart Chain) لتلقي الرصيد عليها فور اعتماد الإدارة:\n\n"
        f"⚠️ **تنبيه:** تأكد أن العنوان يدعم شبكة BNB Smart Chain (BEP20) تحديداً."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    try:
        sent_msg = await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    except Exception:
        sent_msg = await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    await callback.answer()


@deals_router.message(DealPaymentFSM.waiting_for_buyer_wallet)
async def process_set_buyer_wallet(message: Message, state: FSMContext, db_session: AsyncSession):
    wallet_raw = message.text if message.text else ""
    is_valid, normalized_address, val_err = validate_bep20_address(wallet_raw)
    if not is_valid:
        warn_msg = await message.answer(
            f"⚠️ **عنوان محفظة غير صالح!**\n{val_err}\n\n"
            "تأكد أن العنوان يبدأ بـ `0x` ويتكون من 42 خانة ويدعم شبكة **BNB Smart Chain (BEP20)**.\n\n"
            "يرجى إعادة المحاولة أو الإلغاء:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
            ]),
            parse_mode="Markdown"
        )
        await state.update_data(prompt_msg_id=warn_msg.message_id, prompt_chat_id=warn_msg.chat.id)
        return

    data = await state.get_data()
    deal_id = data.get('deal_id')
    if not deal_id:
        await message.answer("حدث خطأ في استعادة بيانات الجلسة.")
        await state.clear()
        return

    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=message.from_user.id,
        expected_statuses=["AWAITING_SYP_PAYMENT", "ADMIN_APPROVAL"],
        allowed_roles="buyer",
        db_session=db_session
    )
    if not valid:
        await db_session.commit()
        await message.answer(err_msg)
        await state.clear()
        return

    if deal.status == "ADMIN_APPROVAL":
        curr_addr = (deal.buyer_payout_address or "").strip()
        if curr_addr and curr_addr not in ["PENDING", "غير محدد", "N/A"]:
            await db_session.commit()
            await message.answer(
                "عنوان المحفظة مسجل مسبقاً ولا يمكن تغييره بعد وصول الصفقة للمراجعة الإدارية."
            )
            await state.clear()
            return

    await state.clear()
    await apply_buyer_payout_wallet(
        deal=deal,
        wallet_address=normalized_address,
        bot=message.bot,
        db_session=db_session,
        event=message
    )


# ================= CONFIRMATION SCREENS FOR CRITICAL ACTIONS =================

@deals_router.callback_query(F.data.startswith("ask_conf_"))
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
        f"⚠️ **تأكيد استلام الدفعة — صفقة #{deal.id}**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• دورك: 🔴 بائع USDT\n"
        f"• المبلغ المطلوب استلامه:  `{syp_str}` SYP\n"
        f"• رصيد الضمان المقابل:  `{usdt_str}` USDT\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"⚠️ **تنبيه:** تأكد من وصول كامل المبلغ لحسابك قبل التأكيد.\n"
        f"بمجرد التأكيد، ستُحال الصفقة تلقائياً للإدارة لإطلاق رصيد الضمان للمشتري ولا يمكن التراجع عن هذا الإجراء."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ نعم، تأكيد استلام الليرة", callback_data=f"man_conf_{deal.id}")],
        [InlineKeyboardButton(text="↩️ رجوع", callback_data=f"back_seller_deal_{deal.id}")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@deals_router.callback_query(F.data.startswith("back_seller_deal_"))
async def cq_back_seller_deal(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()
    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return
    await db_session.commit()

    syp_str = format_syp(deal.amount_syp)
    seller_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ تأكيد استلام الليرة", callback_data=f"ask_conf_{deal.id}")]
    ])
    text = (
        f"🔔 **صفقة #{deal.id} — إشعار تحويل دفعة من المشتري**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• دورك: 🔴 بائع USDT\n"
        f"• المبلغ المحول لحسابك:  `{syp_str}` SYP\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"يرجى فتح تطبيق حسابك والتأكد من وصول المبلغ كاملاً، ثم اضغط على زر التأكيد أدناه:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=seller_kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=seller_kb, parse_mode="Markdown")
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
        [InlineKeyboardButton(text="🚀 اعتماد وإطلاق الضمان", callback_data=f"ask_rel_{deal.id}")],
        [InlineKeyboardButton(text="🔍 فحص تفصيلي للصفقة", callback_data=f"adm_inspect_deal_{deal.id}")]
    ])

    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)

    # 1. Update origin message if present
    if origin_callback:
        try:
            if origin_callback.from_user.id in settings.ADMIN_IDS:
                await origin_callback.message.edit_text(
                    admin_alert_text,
                    reply_markup=admin_kb,
                    parse_mode="Markdown"
                )
            elif origin_callback.from_user.id == deal.seller_id:
                await origin_callback.message.edit_text(
                    f"✅ **صفقة #{deal.id} — تم تأكيد الاستلام وأُحيلت الصفقة تلقائياً للإدارة**\n\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"• دورك: 🔴 بائع USDT\n"
                    f"• المبلغ:  `{usdt_str}` USDT ⇄ `{syp_str}` SYP\n"
                    f"━━━━━━━━━━━━━━━\n\n"
                    f"تم إشعار إدارة {BOT_NAME} لمراجعة الصفقة وإطلاق رصيد الضمان للمشتري.",
                    reply_markup=None,
                    parse_mode="Markdown"
                )
            else:
                await origin_callback.message.edit_text(
                    f"🛡️ **صفقة #{deal.id} — قيد التدقيق والاعتماد الإداري**\n\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"• القيمة:  `{usdt_str}` USDT ⇄ `{syp_str}` SYP\n"
                    f"━━━━━━━━━━━━━━━\n\n"
                    f"تمت إحالة العملية للإدارة للمراجعة وإطلاق رصيد الضمان إلى محفظة المشتري بأمان.",
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.warning(f"Failed to edit origin message in admin dispatch for deal #{deal.id}: {e}")

    # 2. Notify Buyer (single notification)
    if not (origin_callback and origin_callback.from_user.id == deal.buyer_id):
        try:
            await bot.send_message(
                chat_id=deal.buyer_id,
                text=(
                    f"🛡️ **تحديث صفقة #{deal.id}**\n\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"• القيمة:  `{usdt_str}` USDT ⇄ `{syp_str}` SYP\n"
                    f"━━━━━━━━━━━━━━━\n\n"
                    f"أكد البائع استلام كامل حقه بالليرة.\n"
                    f"الصفقة الآن قيد التدقيق والاعتماد الإداري النهائي لإطلاق الرصيد إلى محفظتك."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Failed to notify buyer {deal.buyer_id}: {e}")

    # 3. If buyer wallet is pending, prompt buyer
    if not deal.buyer_payout_address or deal.buyer_payout_address in ["PENDING", "غير محدد", "N/A"]:
        buyer_wallet_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📥 إدخال محفظة الاستلام", callback_data=f"set_buyer_payout_{deal.id}")]
        ])
        try:
            await bot.send_message(
                chat_id=deal.buyer_id,
                text=(
                    f"⚠️ **تنبيه هام — صفقة #{deal.id}**\n\n"
                    f"━━━━━━━━━━━━━━━\n"
                    f"الصفقة قيد الاعتماد للإطلاق.\n"
                    f"يرجى تزويدنا بعنوان محفظتك (BEP-20) عبر الزر أدناه لتلقي الرصيد:"
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


@deals_router.callback_query(F.data.startswith("man_conf_"))
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
    await db_session.commit()

    await _dispatch_admin_approval(deal, callback.bot, db_session, origin_callback=callback)
    await callback.answer()


@deals_router.callback_query(F.data.startswith("man_adm_appr_"))
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


@deals_router.callback_query(F.data.startswith("ask_rel_"))
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
        f"━━━━━━━━━━━━━━━\n"
        f"• الكمية:  `{usdt_str}` USDT\n"
        f"• المقابل:  `{syp_str}` SYP\n"
        f"• محفظة استلام المشتري (BEP-20):\n"
        f"`{buyer_wallet_escaped}`\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"سيتم إطلاق رصيد الضمان فوراً إلى محفظة المشتري وإتمام الصفقة.\n"
        f"⚠️ **تنبيه:** هذا الإجراء نهائي ولا يمكن التراجع عنه. هل أنت متأكد من الإطلاق؟"
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


@deals_router.callback_query(F.data.startswith("back_admin_deal_"))
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
        [InlineKeyboardButton(text="🚀 اعتماد وإطلاق الضمان", callback_data=f"ask_rel_{deal.id}")],
        [InlineKeyboardButton(text="🔍 فحص تفصيلي للصفقة", callback_data=f"adm_inspect_deal_{deal.id}")]
    ])
    try:
        await callback.message.edit_text(admin_text, reply_markup=admin_kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(admin_text, reply_markup=admin_kb, parse_mode="Markdown")
    await callback.answer()


@deals_router.callback_query(F.data.startswith("man_rel_"))
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
    try:
        await callback.message.edit_text(
            f"🎉 **صفقة #{deal.id} — تم الاعتماد والإطلاق بنجاح** ✅\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"• القيمة المطلقة: 💰 `{usdt_str} USDT` ⇄ `{syp_str} SYP`\n"
            f"• محفظة المستلم: 📥 `{deal.buyer_payout_address}`\n"
            f"• معتمد التنفيذ: 👮 `{callback.from_user.id}`\n"
            f"• الحالة النهائية: 🏁 مكتملة بنجاح\n"
            f"━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to edit admin message after release for deal #{deal.id}: {e}")

    # Notify Buyer and Seller of completion
    try:
        await callback.bot.send_message(
            chat_id=deal.buyer_id,
            text=(
                f"🎉 **صفقة #{deal.id} — اكتملت الصفقة بنجاح** ✅\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"• القيمة المستلمة: 💰 `{usdt_str} USDT`\n"
                f"• محفظة الاستلام: 📥 `{deal.buyer_payout_address}`\n"
                f"━━━━━━━━━━━━━━━\n\n"
                f"تمت موافقة الإدارة وإطلاق رصيد الضمان بنجاح إلى محفظتك.\n"
                f"شكراً لتعاملك عبر Alpha Bot."
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify buyer {deal.buyer_id}: {e}")

    try:
        await callback.bot.send_message(
            chat_id=deal.seller_id,
            text=(
                f"🎉 **صفقة #{deal.id} — اكتملت الصفقة بنجاح** ✅\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"• القيمة المبيعة: 💰 `{usdt_str} USDT` ⇄ `{syp_str} SYP`\n"
                f"━━━━━━━━━━━━━━━\n\n"
                f"تم إطلاق رصيد الضمان للمشتري بعد تأكيد استلامك لكامل الدفعة بالليرة.\n"
                f"شكراً لتعاملك عبر Alpha Bot."
            ),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to notify seller {deal.seller_id}: {e}")

@deals_router.message(Command("my_deals"))
async def my_deals(message: Message, db_session: AsyncSession):
    user_id = message.from_user.id
    is_admin = user_id in settings.ADMIN_IDS

    # 1. If admin, fetch all deals awaiting admin approval
    admin_pending_deals = []
    if is_admin:
        admin_res = await db_session.execute(
            select(Deal).where(Deal.status == "ADMIN_APPROVAL").order_by(Deal.created_at.desc())
        )
        admin_pending_deals = admin_res.scalars().all()

    # 2. Fetch user's own deals
    result = await db_session.execute(
        select(Deal).where(
            or_(Deal.buyer_id == user_id, Deal.seller_id == user_id),
            Deal.status.notin_(["COMPLETED", "CANCELLED"])
        ).order_by(Deal.created_at.desc())
    )
    deals = result.scalars().all()
    
    if not deals and not admin_pending_deals:
        await message.answer("ℹ️ ليس لديك أي صفقات نشطة حالياً.")
        return
        
    status_map = {
        "PENDING_OWNER_APPROVAL": "بانتظار موافقة الطرف الآخر",
        "CREATED": "قيد الإنشاء",
        "AWAITING_USDT_DEPOSIT": "بانتظار إيداع الضمان",
        "USDT_DEPOSITED": "الضمان محجوز",
        "AWAITING_SYP_PAYMENT": "بانتظار دفع الليرة",
        "SYP_PAYMENT_SENT": "تم التحويل — بانتظار التأكيد",
        "SYP_PAYMENT_CONFIRMED": "الدفع مؤكد — بانتظار الاعتماد",
        "ADMIN_APPROVAL": "قيد التدقيق والاعتماد",
        "COMPLETED": "مكتملة",
        "CANCELLED": "ملغاة"
    }

    # If admin, show deals awaiting admin approval first
    if admin_pending_deals:
        await message.answer("🛡️ **صفقات بانتظار الاعتماد الإداري للإطلاق:**", parse_mode="Markdown")
        for deal in admin_pending_deals:
            admin_text = await format_admin_deal_review_text(deal, db_session)
            admin_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🚀 اعتماد وإطلاق الضمان", callback_data=f"ask_rel_{deal.id}")]
            ])
            await message.answer(admin_text, reply_markup=admin_kb, parse_mode="Markdown")

    # Show user's personal deals
    if deals:
        if admin_pending_deals:
            await message.answer("📋 **صفقاتك النشطة:**", parse_mode="Markdown")

        for deal in deals:
            role = "🟢 مشتري USDT" if deal.buyer_id == user_id else "🔴 بائع USDT"
            status_ar = status_map.get(deal.status, deal.status)
            usdt_str = format_usdt(deal.amount_usdt)
            syp_str = format_syp(deal.amount_syp)
            
            kb = None
            action_desc = "الصفقة قيد المتابعة."
            
            if deal.status == "CREATED":
                if deal.seller_id == user_id:
                    action_desc = "اضغط أدناه لبدء نافذة الإيداع:"
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🔐 بدء إيداع الضمان", callback_data=f"man_aw_dep_{deal.id}")]
                    ])
                else:
                    action_desc = "بانتظار قيام البائع ببدء نافذة الإيداع."
            elif deal.status == "AWAITING_USDT_DEPOSIT":
                if deal.seller_id == user_id:
                    action_desc = "حوّل الرصيد لمحفظة الضمان ثم اضغط تأكيد الإيداع:"
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✅ تأكيد إيداع الضمان", callback_data=f"man_dep_{deal.id}")]
                    ])
                else:
                    action_desc = "بانتظار قيام البائع بإيداع الضمان."
            elif deal.status == "USDT_DEPOSITED":
                if deal.seller_id == user_id:
                    seller_info_val = deal.seller_payment_info.get("info") if isinstance(deal.seller_payment_info, dict) else ""
                    if not seller_info_val or seller_info_val in ["PENDING", "غير محدد", "N/A"]:
                        action_desc = "تم تأمين الضمان. يرجى تحديد حسابك لاستلام الليرة:"
                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="💳 تحديد حساب استلام الليرة", callback_data=f"set_seller_info_{deal.id}")]
                        ])
                    else:
                        action_desc = "تم حجز الضمان وتحديد الحساب. اضغط للمتابعة:"
                        kb = InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="⏳ متابعة مرحلة الدفع", callback_data=f"man_aw_syp_{deal.id}")]
                        ])
                else:
                    action_desc = "تم تأمين الضمان. بانتظار تزويد البائع لبيانات حسابه."
            elif deal.status == "AWAITING_SYP_PAYMENT":
                if deal.buyer_id == user_id:
                    action_desc = "حوّل المبلغ للبائع ثم اضغط زر التأكيد:"
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✅ تأكيد تحويل الليرة", callback_data=f"man_syp_{deal.id}")]
                    ])
                else:
                    action_desc = "بانتظار قيام المشتري بتحويل المبلغ بالليرة."
            elif deal.status == "SYP_PAYMENT_SENT":
                if deal.seller_id == user_id:
                    action_desc = "أبلغ المشتري عن التحويل. راجع حسابك واضغط تأكيد الاستلام:"
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="✅ تأكيد استلام الليرة", callback_data=f"ask_conf_{deal.id}")]
                    ])
                else:
                    action_desc = "تم الإبلاغ عن التحويل. بانتظار مراجعة البائع وتأكيده."
            elif deal.status == "SYP_PAYMENT_CONFIRMED":
                if deal.seller_id == user_id:
                    action_desc = "أكدت الاستلام. اضغط أدناه لإحالة الصفقة للإدارة:"
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🛡️ طلب اعتماد الإدارة", callback_data=f"man_adm_appr_{deal.id}")]
                    ])
                else:
                    action_desc = "أكد البائع استلام الليرة. الصفقة بانتظار اعتماد الإدارة."
            elif deal.status == "ADMIN_APPROVAL":
                if is_admin:
                    action_desc = "إجراء الإدارة: اضغط أدناه لإطلاق الرصيد:"
                    kb = InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🚀 اعتماد وإطلاق الضمان", callback_data=f"ask_rel_{deal.id}")]
                    ])
                else:
                    action_desc = "الصفقة قيد المراجعة الإدارية لإطلاق الرصيد للمشتري."

            text = (
                f"📑 **صفقة #{deal.id}**\n\n\n"
                f"• الحالة:  **{status_ar}**\n\n"
                f"• دورك في الصفقة:  {role}\n\n"
                f"• الكمية:  `{usdt_str}` USDT\n\n"
                f"• الإجمالي:  `{syp_str}` SYP\n\n\n"
                f"{action_desc}"
            )
            
            await message.answer(text, reply_markup=kb, parse_mode="Markdown")
