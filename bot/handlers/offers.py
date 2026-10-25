import logging
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func

from db.models import Offer, User
from bot.states.flow_states import OfferCreateFSM
from config.settings import settings
from bot.middlewares.rate_limit import check_and_record_rate_limit
from bot.utils.formatters import (
    format_syp,
    format_usdt,
    format_payment_methods,
    AVAILABLE_PAYMENT_METHODS,
    PAYMENT_METHOD_ICONS,
    humanize_time_remaining,
    clean_numeric_input
)

from bot.utils.branding import BOT_NAME, CHANNEL_NAME

logger = logging.getLogger(__name__)

offers_router = Router()

def get_payment_methods_kb(selected_methods: list[str]) -> InlineKeyboardMarkup:
    buttons = []
    for method in AVAILABLE_PAYMENT_METHODS:
        mark = "✅ " if method in selected_methods else "▫️ "
        icon = PAYMENT_METHOD_ICONS.get(method, "💳")
        buttons.append([InlineKeyboardButton(text=f"{mark}{icon} {method}", callback_data=f"pm_toggle_{method}")])
    
    buttons.append([InlineKeyboardButton(text="✨ تأكيد ومتابعة", callback_data="pm_done")])
    buttons.append([InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@offers_router.message(Command("create_offer"))
async def start_offer_creation(message: Message, state: FSMContext, db_session: AsyncSession):
    # Check if user is banned
    user_res = await db_session.execute(
        select(User).where(User.telegram_id == message.from_user.id)
    )
    user = user_res.scalar_one_or_none()
    if user and user.is_banned:
        await message.answer(
            "🚫 **تم تقييد حسابك!**\n\n"
            f"عذراً، حسابك محظور من إنشاء عروض جديدة على {BOT_NAME}.\n"
            "يرجى التواصل مع الدعم الفني لمزيد من التفاصيل.",
            parse_mode="Markdown"
        )
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔴 بيع USDT", callback_data="offer_type_sell"),
            InlineKeyboardButton(text="🟢 شراء USDT", callback_data="offer_type_buy")
        ],
        [
            InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")
        ]
    ])
    await message.answer(
        "📝 **إنشاء عرض تداول جديد**\n\n"
        "━━━━━━━━━━━━━━━\n"
        "حدد نوع العملية التي ترغب بطرحها في السوق:\n\n"
        "• 🔴 **بيع USDT**: تقديم USDT واستلام ليرة سورية (SYP).\n"
        "• 🟢 **شراء USDT**: دفع ليرة سورية (SYP) واستلام USDT.\n"
        "━━━━━━━━━━━━━━━\n\n"
        "اختر النوع للمتابعة:",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_type)

@offers_router.callback_query(OfferCreateFSM.waiting_for_type, F.data.startswith("offer_type_"))
async def process_offer_type(callback: CallbackQuery, state: FSMContext):
    offer_type = callback.data.split("_")[-1]
    offer_type_ar = "🔴 بيع USDT" if offer_type == "sell" else "🟢 شراء USDT"
    await state.update_data(offer_type=offer_type)
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    await callback.message.edit_text(
        f"📝 **إنشاء عرض جديد — {offer_type_ar}**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💵 أدخل سعر الصرف بالليرة السورية مقابل 1 USDT:\n"
        f"*(مثال: 15200)*\n"
        f"━━━━━━━━━━━━━━━",
        reply_markup=cancel_kb,
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_rate)

@offers_router.message(OfferCreateFSM.waiting_for_rate)
async def process_offer_rate(message: Message, state: FSMContext):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    try:
        rate = clean_numeric_input(message.text)
        if not rate or rate <= 0:
            raise ValueError()
        await state.update_data(rate=str(rate))
        rate_str = format_syp(rate)
        await message.answer(
            f"📝 **إنشاء عرض** | السعر المعتمد: 💵 `{rate_str} SYP`\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📦 أدخل الحد الأدنى للكمية المسموح بها في الصفقة (USDT):\n"
            f"*(مثال: 50)*\n"
            f"━━━━━━━━━━━━━━━",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        await state.set_state(OfferCreateFSM.waiting_for_min_limit)
    except Exception:
        await message.answer(
            "⚠️ **سعر الصرف غير صالح!**\n\n"
            "يرجى إدخال رقم صحيح وموجب (مثال: `15200`):",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )

@offers_router.message(OfferCreateFSM.waiting_for_min_limit)
async def process_min_limit(message: Message, state: FSMContext):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    try:
        min_limit = clean_numeric_input(message.text)
        if not min_limit or min_limit <= 0:
            raise ValueError()
        await state.update_data(min_limit=str(min_limit))
        min_str = format_usdt(min_limit)
        await message.answer(
            f"📝 **إنشاء عرض** | الحد الأدنى: 📦 `{min_str} USDT`\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📦 أدخل الحد الأقصى للكمية الإجمالية المعروضة:\n"
            f"*(يجب أن يكون أكبر من أو يساوي `{min_str}` USDT)*\n"
            f"━━━━━━━━━━━━━━━",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        await state.set_state(OfferCreateFSM.waiting_for_max_limit)
    except Exception:
        await message.answer(
            "⚠️ **قيمة غير صالحة!**\n\n"
            "يرجى إدخال رقم موجب وصحيح بوحدة USDT:",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )

@offers_router.message(OfferCreateFSM.waiting_for_max_limit)
async def process_max_limit(message: Message, state: FSMContext):
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    try:
        max_limit = clean_numeric_input(message.text)
        if not max_limit or max_limit <= 0:
            raise ValueError()
        data = await state.get_data()
        min_limit = Decimal(data['min_limit'])
        
        if max_limit < min_limit:
            await message.answer(
                f"⚠️ **الحد الأقصى غير متطابق!**\n\n"
                f"يجب أن يكون أكبر من أو يساوي الحد الأدنى (`{format_usdt(min_limit)}` USDT).\n"
                f"يرجى إعادة إدخال الحد الأقصى:",
                reply_markup=cancel_kb,
                parse_mode="Markdown"
            )
            return
            
        await state.update_data(max_limit=str(max_limit), payment_methods=[])
        
        kb = get_payment_methods_kb([])
        await message.answer(
            "💳 **وسائل الدفع المقبولة**\n\n"
            "━━━━━━━━━━━━━━━\n"
            "اضغط على وسائل الدفع المقبولة لديك لتحديدها (يمكنك اختيار أكثر من وسيلة)، ثم اضغط على زر التأكيد:\n"
            "━━━━━━━━━━━━━━━",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        await state.set_state(OfferCreateFSM.waiting_for_methods)
    except Exception:
        await message.answer(
            "⚠️ **قيمة غير صالحة!**\n\n"
            "يرجى إدخال رقم صحيح وموجب للحد الأقصى بوحدة USDT:",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )

@offers_router.callback_query(OfferCreateFSM.waiting_for_methods, F.data.startswith("pm_toggle_"))
async def toggle_payment_method(callback: CallbackQuery, state: FSMContext):
    method = callback.data.split("pm_toggle_")[1]
    data = await state.get_data()
    selected_methods = data.get("payment_methods", [])
    
    if method in selected_methods:
        selected_methods.remove(method)
    else:
        selected_methods.append(method)
        
    await state.update_data(payment_methods=selected_methods)
    kb = get_payment_methods_kb(selected_methods)
    await callback.message.edit_reply_markup(reply_markup=kb)

@offers_router.callback_query(OfferCreateFSM.waiting_for_methods, F.data == "pm_done")
async def confirm_payment_methods(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected_methods = data.get("payment_methods", [])
    
    if not selected_methods:
        await callback.answer("⚠️ يرجى اختيار طريقة دفع واحدة على الأقل للمتابعة.", show_alert=True)
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤝 إتاحة التفاوض", callback_data="neg_choice_yes"),
            InlineKeyboardButton(text="🔒 شروط ثابتة فقط", callback_data="neg_choice_no")
        ],
        [
            InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")
        ]
    ])
    await callback.message.edit_text(
        "⚙️ **خيارات التفاوض على العرض**\n\n"
        "━━━━━━━━━━━━━━━\n"
        "هل ترغب بالسماح للمتداولين بتقديم اقتراحات تفاوض على السعر أو الكمية؟\n"
        "━━━━━━━━━━━━━━━\n\n"
        "اختر الإعداد المناسب:",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_negotiable_decision)

@offers_router.callback_query(OfferCreateFSM.waiting_for_negotiable_decision, F.data.startswith("neg_choice_"))
async def process_negotiable_decision(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split("_")[-1]
    if choice == "yes":
        await state.update_data(negotiable=True)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 على الكمية فقط", callback_data="neg_type_amount")],
            [InlineKeyboardButton(text="💵 على السعر فقط", callback_data="neg_type_rate")],
            [InlineKeyboardButton(text="⚖️ على السعر والكمية معاً", callback_data="neg_type_both")],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
        ])
        await callback.message.edit_text(
            "⚙️ **تحديد نطاق التفاوض**\n\n"
            "━━━━━━━━━━━━━━━\n"
            "حدد البنود التي تسمح للطرف الآخر بالتفاوض عليها:\n"
            "━━━━━━━━━━━━━━━",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        await state.set_state(OfferCreateFSM.waiting_for_negotiable_type)
    else:
        await state.update_data(negotiable=False, negotiable_type="none")
        await _show_duration_choice(callback.message, state)

@offers_router.callback_query(OfferCreateFSM.waiting_for_negotiable_type, F.data.startswith("neg_type_"))
async def process_negotiable_type(callback: CallbackQuery, state: FSMContext):
    neg_type = callback.data.replace("neg_type_", "")
    await state.update_data(negotiable_type=neg_type)
    await _show_duration_choice(callback.message, state)

async def _show_duration_choice(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱️ 4 ساعات (الافتراضي)", callback_data="dur_default_4")],
        [InlineKeyboardButton(text="⚙️ تحديد مدة مخصصة (1-4 ساعات)", callback_data="dur_custom")],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    await message.edit_text(
        "⏱️ **مدة صلاحية العرض في السوق**\n\n"
        "━━━━━━━━━━━━━━━\n"
        "اختر مدة بقاء عرضك متاحاً ونشطاً في السوق قبل انتهاء صلاحيته آلياً:\n"
        "━━━━━━━━━━━━━━━",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_duration_choice)

@offers_router.callback_query(OfferCreateFSM.waiting_for_duration_choice, F.data.startswith("dur_"))
async def process_duration_choice(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession, bot: Bot):
    if callback.data == "dur_default_4":
        await _create_and_publish_offer(callback.message, state, db_session, bot, hours_val=4.0, user_id=callback.from_user.id)
    else:
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
        ])
        await callback.message.edit_text(
            "⏱️ **تحديد مدة العرض المخصصة**\n\n"
            "━━━━━━━━━━━━━━━\n"
            "أدخل عدد الساعات المطلوب لبقاء العرض نشطاً (رقم من 1 إلى 4):\n"
            "━━━━━━━━━━━━━━━",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        await state.set_state(OfferCreateFSM.waiting_for_custom_duration)

@offers_router.message(OfferCreateFSM.waiting_for_custom_duration)
async def process_custom_duration(message: Message, state: FSMContext, db_session: AsyncSession, bot: Bot):
    try:
        hours_val = float(message.text.strip())
        if hours_val <= 0 or hours_val > 4:
            await message.answer(
                "⚠️ يرجى إدخال رقم بين 1 و 4 ساعات:",
                parse_mode="Markdown"
            )
            return
    except Exception:
        await message.answer(
            "⚠️ يرجى إرسال رقم صحيح لعدد الساعات (مثال: `2` أو `3`):",
            parse_mode="Markdown"
        )
        return

    await _create_and_publish_offer(message, state, db_session, bot, hours_val=hours_val, user_id=message.from_user.id)

async def _create_and_publish_offer(message: Message, state: FSMContext, db_session: AsyncSession, bot: Bot, hours_val: float, user_id: int):
    # Section 6.1.1: Active offers cap check (max 5 active offers)
    if user_id not in settings.ADMIN_IDS:
        count_res = await db_session.execute(
            select(func.count(Offer.id)).where(
                Offer.user_id == user_id,
                Offer.status.in_(['active', 'reserved', 'negotiating', 'expired_grace'])
            )
        )
        active_offers_count = count_res.scalar() or 0
        if active_offers_count >= 5:
            await message.answer(
                "⚠️ **تجاوزت الحد الأقصى للعروض النشطة!**\n\n"
                "━━━━━━━━━━━━━━━\n"
                "الحد الأقصى المسموح به هو **5 عروض نشطة** في نفس الوقت.\n"
                "يرجى حذف أو إنهاء أحد عروضك السابقة أولاً قبل إنشاء عرض جديد.",
                parse_mode="Markdown"
            )
            await state.clear()
            return

        # Section 6.1.2: Rolling window rate limit (max 3 offers per 10 mins)
        is_allowed, retry_after = check_and_record_rate_limit(user_id, "create_offer")
        if not is_allowed:
            minutes = (retry_after + 59) // 60
            await message.answer(
                f"⚠️ **تجاوزت الحد الزمني لإنشاء العروض!**\n\n"
                f"━━━━━━━━━━━━━━━\n"
                f"الحد الأقصى المسموح به هو **3 عمليات إنشاء عروض لكل 10 دقائق**.\n"
                f"يرجى الانتظار حوالي **{minutes} دقيقة** والمحاولة مجدداً.",
                parse_mode="Markdown"
            )
            await state.clear()
            return

    data = await state.get_data()
    selected_methods = data.get("payment_methods", [])
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(hours=hours_val)
    
    new_offer = Offer(
        user_id=user_id,
        offer_type=data['offer_type'],
        rate=Decimal(data['rate']),
        min_limit=Decimal(data['min_limit']),
        max_limit=Decimal(data['max_limit']),
        payment_methods=selected_methods,
        payout_info="",
        negotiable=data.get('negotiable', False),
        negotiable_type=data.get('negotiable_type', 'none'),
        expires_at=expires_at,
        status="active"
    )
    db_session.add(new_offer)
    await db_session.commit()
    await db_session.refresh(new_offer)
    
    duration_str = f"{int(hours_val)} ساعات" if hours_val == int(hours_val) else f"{hours_val} ساعة"
    rate_str = format_syp(new_offer.rate)
    min_str = format_usdt(new_offer.min_limit)
    max_str = format_usdt(new_offer.max_limit)
    offer_type_ar = "🔴 بيع USDT" if new_offer.offer_type == 'sell' else "🟢 شراء USDT"
    methods_str = format_payment_methods(new_offer.payment_methods)
    
    neg_type_labels = {
        "none": "غير متاح 🔒",
        "amount": "متاح على الكمية 📦",
        "rate": "متاح على السعر 💵",
        "both": "متاح على السعر والكمية ⚖️"
    }
    neg_status_ar = neg_type_labels.get(new_offer.negotiable_type, "غير متاح 🔒")

    await message.answer(
        f"🎉 **تم نشر العرض بنجاح — عرض #{new_offer.id}** ✅\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• النوع: {offer_type_ar}\n"
        f"• سعر الصرف: 💵 `{rate_str} SYP`\n"
        f"• حدود الكمية: 📦 من `{min_str}` إلى `{max_str}` USDT\n"
        f"• وسائل الدفع: 💳 {methods_str}\n"
        f"• حالة التفاوض: {neg_status_ar}\n"
        f"• مدة الصلاحية: ⏱️ {duration_str}\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"تم إدراج العرض بنجاح في سوق {BOT_NAME} وقناة {CHANNEL_NAME}.",
        parse_mode="Markdown"
    )
    await state.clear()

    # Broadcast to Channel
    if settings.CHANNEL_ID and settings.BOT_USERNAME:
        action_ar = "شراء" if new_offer.offer_type == 'sell' else "بيع"
        neg_line = f"\n• <b>التفاوض:</b> {neg_status_ar}" if new_offer.negotiable else ""
        
        channel_text = (
            f"\u200f📢 <b>{CHANNEL_NAME} | عرض تداول #{new_offer.id} — {offer_type_ar}</b>\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"• <b>سعر الصرف:</b> 💵 <code>{rate_str} SYP</code>\n"
            f"• <b>حدود الكمية:</b> 📦 <code>{min_str}</code> — <code>{max_str}</code> USDT\n"
            f"• <b>وسائل الدفع:</b> 💳 {methods_str}{neg_line}\n"
            f"• <b>المهلة المتاحة:</b> ⏱️ {duration_str}\n"
            f"━━━━━━━━━━━━━━━"
        )
        
        deep_link = f"https://t.me/{settings.BOT_USERNAME}?start=offer_{new_offer.id}"
        kb_chan = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⚡ بدء تنفيذ العرض ({action_ar})", url=deep_link)]
        ])
        
        try:
            msg = await bot.send_message(
                chat_id=settings.CHANNEL_ID,
                text=channel_text,
                reply_markup=kb_chan,
                parse_mode="HTML"
            )
            new_offer.channel_message_id = msg.message_id
            await db_session.commit()
        except Exception as e:
            print(f"Failed to broadcast to channel: {e}")

@offers_router.message(Command("offers"))
async def list_offers(message: Message, db_session: AsyncSession):
    result = await db_session.execute(select(Offer).where(Offer.status == "active").order_by(Offer.created_at.desc()).limit(10))
    offers = result.scalars().all()
    
    if not offers:
        await message.answer(
            "🏪 **سوق التداول المباشر**\n\n"
            "━━━━━━━━━━━━━━━\n"
            "لا توجد عروض نشطة في السوق حالياً.\n"
            "يمكنك إنشاء أول عرض الآن عبر الأمر /create_offer أو من القائمة الرئيسية.\n"
            "━━━━━━━━━━━━━━━",
            parse_mode="Markdown"
        )
        return
        
    for offer in offers:
        buttons = [
            [InlineKeyboardButton(text="⚡ تنفيذ فوري بالسعر الحالي", callback_data=f"start_deal_{offer.id}")]
        ]
        if offer.negotiable and offer.negotiable_type != "none":
            buttons.append([InlineKeyboardButton(text="🤝 تقديم طلب تفاوض", callback_data=f"start_neg_{offer.id}")])

        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        rate_str = format_syp(offer.rate)
        min_str = format_usdt(offer.min_limit)
        max_str = format_usdt(offer.max_limit)
        offer_type_ar = "🔴 بيع USDT" if offer.offer_type == 'sell' else "🟢 شراء USDT"
        methods_str = format_payment_methods(offer.payment_methods)
        remaining_str = humanize_time_remaining(offer.expires_at, "4 ساعات")
        
        neg_type_labels = {
            "none": "غير متاح 🔒",
            "amount": "متاح (على الكمية) 📦",
            "rate": "متاح (على السعر) 💵",
            "both": "متاح (على السعر والكمية) ⚖️"
        }
        neg_str = neg_type_labels.get(offer.negotiable_type, "غير متاح 🔒") if offer.negotiable else "غير متاح 🔒"

        text = (
            f"🏷️ **عرض #{offer.id}** — {offer_type_ar}\n\n\n"
            f"• سعر الصرف:  `{rate_str}` SYP\n\n"
            f"• حدود الكمية:  من `{min_str}` إلى `{max_str}` USDT\n\n"
            f"• طرق الدفع:  {methods_str}\n\n"
            f"• حالة التفاوض:  {neg_str}\n\n"
            f"• الصلاحية المتبقية:  {remaining_str}"
        )
        await message.answer(text, reply_markup=kb, parse_mode="Markdown")

@offers_router.callback_query(F.data.startswith("del_off_"))
async def handle_delete_offer(callback: CallbackQuery, db_session: AsyncSession):
    offer_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(
        select(Offer).where(Offer.id == offer_id).with_for_update()
    )
    offer = result.scalar_one_or_none()

    if not offer:
        await callback.answer("عذراً، لم يتم العثور على العرض.", show_alert=True)
        return

    if callback.from_user.id != offer.user_id and callback.from_user.id not in settings.ADMIN_IDS:
        await callback.answer("غير مصرح: هذا الإجراء لصاحب العرض فقط.", show_alert=True)
        return

    from bot.utils.transitions import can_delete_offer
    can_del, reason = can_delete_offer(offer.status)
    if not can_del:
        await db_session.commit()
        await callback.answer(reason, show_alert=True)
        return

    offer.status = "cancelled"
    await db_session.commit()

    from bot.handlers.deals import update_channel_offer_message
    try:
        await update_channel_offer_message(callback.bot, offer)
    except Exception as e:
        logger.warning(f"Failed to update channel message for deleted offer #{offer.id}: {e}")

    await callback.answer("تم إلغاء العرض بنجاح.")
    await callback.message.edit_text(
        f"❌ **عرض #{offer.id} — تم إلغاء العرض**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"تم إلغاء العرض بنجاح وإزالته من السوق النشط والقناة الرسمية.",
        parse_mode="Markdown"
    )

@offers_router.callback_query(F.data.startswith("ext_off_"))
async def handle_extend_offer_prompt(callback: CallbackQuery, db_session: AsyncSession):
    offer_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(
        select(Offer).where(Offer.id == offer_id).with_for_update()
    )
    offer = result.scalar_one_or_none()

    if not offer:
        await callback.answer("عذراً، لم يتم العثور على العرض.", show_alert=True)
        return

    if callback.from_user.id != offer.user_id and callback.from_user.id not in settings.ADMIN_IDS:
        await callback.answer("غير مصرح: هذا الإجراء لصاحب العرض فقط.", show_alert=True)
        return

    from bot.utils.transitions import can_extend_offer
    can_ext, reason = can_extend_offer(offer.status)
    if not can_ext:
        await db_session.commit()
        await callback.answer(reason, show_alert=True)
        return

    # Release row lock before Telegram UI call without expiring attributes
    await db_session.commit()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏱️ 1 ساعة", callback_data=f"ext_dur_{offer.id}_1"),
            InlineKeyboardButton(text="⏱️ 2 ساعة", callback_data=f"ext_dur_{offer.id}_2"),
        ],
        [
            InlineKeyboardButton(text="⏱️ 3 ساعات", callback_data=f"ext_dur_{offer.id}_3"),
            InlineKeyboardButton(text="⏱️ 4 ساعات", callback_data=f"ext_dur_{offer.id}_4"),
        ],
        [
            InlineKeyboardButton(text="❌ تراجع", callback_data=f"ext_cancel_{offer.id}")
        ]
    ])

    await callback.message.edit_text(
        f"⏱️ **تمديد صلاحية عرض #{offer.id}**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"اختر المدة الإضافية التي ترغب ببقاء العرض نشطاً خلالها في السوق:\n"
        f"━━━━━━━━━━━━━━━",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await callback.answer()

@offers_router.callback_query(F.data.startswith("ext_dur_"))
async def handle_extend_offer_duration(callback: CallbackQuery, db_session: AsyncSession):
    # format: ext_dur_{offer_id}_{hours}
    parts = callback.data.split("_")
    offer_id = int(parts[2])
    hours = int(parts[3])

    result = await db_session.execute(
        select(Offer).where(Offer.id == offer_id).with_for_update()
    )
    offer = result.scalar_one_or_none()

    if not offer:
        await callback.answer("عذراً، لم يتم العثور على العرض.", show_alert=True)
        return

    if callback.from_user.id != offer.user_id and callback.from_user.id not in settings.ADMIN_IDS:
        await callback.answer("غير مصرح: هذا الإجراء لصاحب العرض فقط.", show_alert=True)
        return

    from bot.utils.transitions import can_extend_offer
    can_ext, reason = can_extend_offer(offer.status)
    if not can_ext:
        await db_session.commit()
        await callback.answer(reason, show_alert=True)
        return

    now = datetime.now(timezone.utc)
    offer.status = "active"
    offer.expires_at = now + timedelta(hours=hours)
    await db_session.commit()

    from bot.handlers.deals import update_channel_offer_message
    try:
        await update_channel_offer_message(callback.bot, offer)
    except Exception as e:
        logger.warning(f"Failed to update channel message for extended offer #{offer.id}: {e}")

    expires_str = offer.expires_at.strftime('%H:%M UTC')
    await callback.answer("تم تمديد صلاحية العرض بنجاح.")
    await callback.message.edit_text(
        f"✅ **عرض #{offer.id} — تم تمديد الصلاحية بنجاح**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• المدة الإضافية: ⏱️ `{hours}` ساعة\n"
        f"• الصلاحية حتى: ⏰ `{expires_str}`\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"العرض نشط ومتاح حالياً في السوق والقناة الرسمية.",
        parse_mode="Markdown"
    )

@offers_router.callback_query(F.data.startswith("ext_cancel_"))
async def handle_extend_cancel(callback: CallbackQuery):
    offer_id = int(callback.data.split("_")[-1])
    grace_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏱️ تمديد الصلاحية", callback_data=f"ext_off_{offer_id}"),
            InlineKeyboardButton(text="❌ إلغاء العرض", callback_data=f"del_off_{offer_id}")
        ]
    ])
    await callback.message.edit_text(
        f"⚠️ **عرض #{offer_id} في فترة السماح**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"العرض مخفي مؤقتاً من السوق (مهلة سماح 30 دقيقة).\n"
        f"اختر الإجراء المطلوب لمتابعته:\n"
        f"━━━━━━━━━━━━━━━",
        reply_markup=grace_kb,
        parse_mode="Markdown"
    )
    await callback.answer()
