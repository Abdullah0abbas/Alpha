import logging
import json
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
    clean_numeric_input,
    format_local_datetime
)
from bot.utils.validators import (
    validate_bep20_address,
    validate_payment_method_info
)
from bot.utils.offer_wizard import (
    push_wizard_step,
    pop_wizard_step,
    extract_primary_payout_info,
    is_wizard_data_complete,
    format_wizard_summary,
    FEE_RULE_LINE
)
from bot.utils.fees import format_fee_rule_line, format_fee_rule_html
from bot.utils.branding import BOT_NAME, CHANNEL_NAME

logger = logging.getLogger(__name__)

offers_router = Router()


# ==============================================================================
# KEYBOARD HELPERS
# ==============================================================================

def get_confirm_step_kb(confirm_cb: str, edit_cb: str, back_cb: str | None = None) -> InlineKeyboardMarkup:
    """Standard confirmation keyboard with Confirm, Edit, Back, and Cancel."""
    rows = [
        [
            InlineKeyboardButton(text="✅ تأكيد ومتابعة", callback_data=confirm_cb),
            InlineKeyboardButton(text="✏️ تعديل", callback_data=edit_cb)
        ]
    ]
    nav_row = []
    if back_cb:
        nav_row.append(InlineKeyboardButton(text="🔙 رجوع", callback_data=back_cb))
    nav_row.append(InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel"))
    rows.append(nav_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_step_nav_kb(back_cb: str | None = None) -> InlineKeyboardMarkup:
    """Standard navigation keyboard with Back and Cancel."""
    nav_row = []
    if back_cb:
        nav_row.append(InlineKeyboardButton(text="🔙 رجوع", callback_data=back_cb))
    nav_row.append(InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel"))
    return InlineKeyboardMarkup(inline_keyboard=[nav_row])


def get_cancel_confirm_kb() -> InlineKeyboardMarkup:
    """Confirmation keyboard before aborting wizard and discarding draft."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="نعم، إلغاء العرض", callback_data="wiz_cancel_confirm_yes"),
            InlineKeyboardButton(text="لا، متابعة الإنشاء", callback_data="wiz_cancel_confirm_no")
        ]
    ])


def get_payment_methods_kb(selected_methods: list[str]) -> InlineKeyboardMarkup:
    """Payment methods multi-select keyboard with Back and Cancel."""
    buttons = []
    for method in AVAILABLE_PAYMENT_METHODS:
        mark = "✅ " if method in selected_methods else "▫️ "
        icon = PAYMENT_METHOD_ICONS.get(method, "💳")
        buttons.append([InlineKeyboardButton(text=f"{mark}{icon} {method}", callback_data=f"pm_toggle_{method}")])

    buttons.append([InlineKeyboardButton(text="✨ تأكيد ومتابعة", callback_data="pm_done")])
    buttons.append([
        InlineKeyboardButton(text="🔙 رجوع", callback_data="wiz_back_methods"),
        InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_wizard_summary_kb() -> InlineKeyboardMarkup:
    """Summary keyboard with Publish, jump-edit buttons, Back, and Cancel."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 تأكيد ونشر العرض في السوق", callback_data="wiz_publish")],
        [
            InlineKeyboardButton(text="✏️ السعر", callback_data="wiz_jump_rate"),
            InlineKeyboardButton(text="✏️ الكميات", callback_data="wiz_jump_limits")
        ],
        [
            InlineKeyboardButton(text="✏️ وسائل الدفع", callback_data="wiz_jump_methods"),
            InlineKeyboardButton(text="✏️ بيانات الاستلام", callback_data="wiz_jump_payout")
        ],
        [
            InlineKeyboardButton(text="✏️ التفاوض", callback_data="wiz_jump_neg"),
            InlineKeyboardButton(text="✏️ مدة الصلاحية", callback_data="wiz_jump_dur")
        ],
        [
            InlineKeyboardButton(text="🔙 رجوع للخطوة السابقة", callback_data="wiz_back_summary"),
            InlineKeyboardButton(text="❌ إلغاء العرض", callback_data="wiz_cancel")
        ]
    ])


# ==============================================================================
# CANCEL CONFIRMATION DIALOG (GATEKEEPER)
# ==============================================================================

@offers_router.callback_query(
    OfferCreateFSM(),
    F.data.in_(["wiz_cancel", "fsm_cancel_back"])
)
async def ask_cancel_wizard(callback: CallbackQuery, state: FSMContext):
    """Prompt user before discarding draft data."""
    cur_state = await state.get_state()
    await state.update_data(cancel_origin_state=cur_state)
    await state.set_state(OfferCreateFSM.confirm_cancel)
    await callback.message.edit_text(
        "⚠️ **تأكيد إلغاء إنشاء العرض**\n\n"
        "━━━━━━━━━━━━━━━\n"
        "هل أنت متأكد من رغبتك في إلغاء عملية إنشاء العرض وحذف كافة البيانات المدخلة؟\n"
        "━━━━━━━━━━━━━━━",
        reply_markup=get_cancel_confirm_kb(),
        parse_mode="Markdown"
    )
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.confirm_cancel, F.data == "wiz_cancel_confirm_yes")
async def process_cancel_confirm_yes(callback: CallbackQuery, state: FSMContext):
    """Discard wizard draft data and clear FSM state."""
    await state.clear()
    await callback.message.edit_text(
        "❌ **تم إلغاء عملية إنشاء العرض.**\n\n"
        "تم مسح كافة البيانات المؤقتة. يمكنك البدء من جديد عبر الأمر /create_offer أو من القائمة الرئيسية.",
        parse_mode="Markdown"
    )
    await callback.answer("تم إلغاء العملية.")


@offers_router.callback_query(OfferCreateFSM.confirm_cancel, F.data == "wiz_cancel_confirm_no")
async def process_cancel_confirm_no(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    """Return user back to their active wizard step without data loss."""
    data = await state.get_data()
    origin = data.get("cancel_origin_state") or OfferCreateFSM.waiting_for_type.state
    await state.set_state(origin)
    await callback.answer("تمت استعادة خطوات الإنشاء.")
    await _render_current_step(callback.message, state, db_session, user_id=callback.from_user.id)


async def _render_current_step(message: Message, state: FSMContext, db_session: AsyncSession, user_id: int):
    """Re-render current step UI based on current FSM state."""
    cur_state = await state.get_state()
    data = await state.get_data()

    if cur_state == OfferCreateFSM.waiting_for_type.state:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔴 بيع USDT", callback_data="offer_type_sell"),
                InlineKeyboardButton(text="🟢 شراء USDT", callback_data="offer_type_buy")
            ],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")]
        ])
        await message.edit_text(
            "📝 **إنشاء عرض تداول جديد**\n\n"
            "━━━━━━━━━━━━━━━\n"
            "حدد نوع العملية التي ترغب بطرحها في السوق:\n\n"
            "• 🔴 **بيع USDT**: تقديم USDT واستلام ليرة سورية.\n"
            "• 🟢 **شراء USDT**: دفع ليرة سورية واستلام USDT.\n"
            "━━━━━━━━━━━━━━━\n\n"
            "اختر النوع للمتابعة:",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    elif cur_state in (OfferCreateFSM.waiting_for_rate.state, OfferCreateFSM.confirm_rate.state):
        offer_type_ar = "🔴 بيع USDT" if data.get("offer_type") == "sell" else "🟢 شراء USDT"
        await message.edit_text(
            f"📝 **إنشاء عرض جديد — {offer_type_ar}**\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"💵 أدخل سعر الصرف بالليرة السورية مقابل 1 USDT:\n"
            f"━━━━━━━━━━━━━━━",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_rate"),
            parse_mode="Markdown"
        )
    elif cur_state in (OfferCreateFSM.waiting_for_min_limit.state, OfferCreateFSM.confirm_min_limit.state):
        await message.edit_text(
            "📝 **إنشاء عرض**\n\n"
            "━━━━━━━━━━━━━━━\n"
            "📦 أدخل الحد الأدنى للكمية المسموح بها في الصفقة (USDT):\n"
            "━━━━━━━━━━━━━━━",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_min"),
            parse_mode="Markdown"
        )
    elif cur_state in (OfferCreateFSM.waiting_for_max_limit.state, OfferCreateFSM.confirm_max_limit.state):
        min_str = format_usdt(Decimal(str(data.get("min_limit", "0"))))
        await message.edit_text(
            f"📝 **إنشاء عرض** | الحد الأدنى: 📦 `{min_str} USDT`\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📦 أدخل الحد الأقصى للكمية الإجمالية المعروضة:\n"
            f"*(يجب أن يكون أكبر من أو يساوي `{min_str}` USDT)*\n"
            f"━━━━━━━━━━━━━━━",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_max"),
            parse_mode="Markdown"
        )
    elif cur_state == OfferCreateFSM.waiting_for_methods.state:
        selected = data.get("payment_methods", [])
        await message.edit_text(
            "💳 **وسائل الدفع المقبولة**\n\n"
            "━━━━━━━━━━━━━━━\n"
            "اضغط على وسائل الدفع المقبولة لديك لتحديدها (يمكنك اختيار أكثر من وسيلة)، ثم اضغط على زر التأكيد:\n"
            "━━━━━━━━━━━━━━━",
            reply_markup=get_payment_methods_kb(selected),
            parse_mode="Markdown"
        )
    elif cur_state in (OfferCreateFSM.waiting_for_payout_details.state, OfferCreateFSM.confirm_payout_details.state):
        await _show_payout_prompt(message, state, db_session, user_id=user_id)
    elif cur_state in (OfferCreateFSM.waiting_for_negotiable_decision.state, OfferCreateFSM.waiting_for_negotiable_type.state):
        await _show_negotiation_decision(message)
    elif cur_state in (OfferCreateFSM.waiting_for_duration_choice.state, OfferCreateFSM.waiting_for_custom_duration.state, OfferCreateFSM.confirm_custom_duration.state):
        await _show_duration_choice(message)
    elif cur_state == OfferCreateFSM.waiting_for_confirm_publish.state:
        await _show_wizard_summary(message, state)


# ==============================================================================
# STEP 1: OFFER TYPE (SELL / BUY)
# ==============================================================================

@offers_router.message(Command("create_offer"))
async def start_offer_creation(message: Message, state: FSMContext, db_session: AsyncSession):
    """Entry point for creating a new offer."""
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

    await state.clear()
    await state.update_data(
        step_history=[],
        payout_details={},
        payment_methods=[]
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔴 بيع USDT", callback_data="offer_type_sell"),
            InlineKeyboardButton(text="🟢 شراء USDT", callback_data="offer_type_buy")
        ],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")]
    ])
    await message.answer(
        "📝 **إنشاء عرض تداول جديد**\n\n"
        "━━━━━━━━━━━━━━━\n"
        "حدد نوع العملية التي ترغب بطرحها في السوق:\n\n"
        "• 🔴 **بيع USDT**: تقديم USDT واستلام ليرة سورية.\n"
        "• 🟢 **شراء USDT**: دفع ليرة سورية واستلام USDT.\n"
        "━━━━━━━━━━━━━━━\n\n"
        "اختر النوع للمتابعة:",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_type)


@offers_router.callback_query(OfferCreateFSM.waiting_for_type, F.data.startswith("offer_type_"))
async def process_offer_type(callback: CallbackQuery, state: FSMContext):
    """Capture offer type and advance to rate input."""
    offer_type = callback.data.split("_")[-1]
    offer_type_ar = "🔴 بيع USDT" if offer_type == "sell" else "🟢 شراء USDT"
    data = await state.get_data()
    history = push_wizard_step(data.get("step_history", []), "type", {"offer_type": offer_type})

    await state.update_data(
        offer_type=offer_type,
        step_history=history
    )
    await callback.message.edit_text(
        f"📝 **إنشاء عرض جديد — {offer_type_ar}**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💵 أدخل سعر الصرف بالليرة السورية مقابل 1 USDT:\n"
        f"━━━━━━━━━━━━━━━",
        reply_markup=get_step_nav_kb(back_cb="wiz_back_rate"),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_rate)
    await callback.answer()


# ==============================================================================
# STEP 2: EXCHANGE RATE & CONFIRMATION
# ==============================================================================

@offers_router.callback_query(OfferCreateFSM.waiting_for_rate, F.data == "wiz_back_rate")
async def back_to_offer_type(callback: CallbackQuery, state: FSMContext):
    """Navigate back from Rate input to Offer Type."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔴 بيع USDT", callback_data="offer_type_sell"),
            InlineKeyboardButton(text="🟢 شراء USDT", callback_data="offer_type_buy")
        ],
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")]
    ])
    await callback.message.edit_text(
        "📝 **إنشاء عرض تداول جديد**\n\n"
        "━━━━━━━━━━━━━━━\n"
        "حدد نوع العملية التي ترغب بطرحها في السوق:\n\n"
        "• 🔴 **بيع USDT**: تقديم USDT واستلام ليرة سورية.\n"
        "• 🟢 **شراء USDT**: دفع ليرة سورية واستلام USDT.\n"
        "━━━━━━━━━━━━━━━\n\n"
        "اختر النوع للمتابعة:",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_type)
    await callback.answer()


@offers_router.message(OfferCreateFSM.waiting_for_rate)
async def process_offer_rate(message: Message, state: FSMContext):
    """Receive and validate exchange rate, then proceed directly to min limit or summary."""
    try:
        rate = clean_numeric_input(message.text)
        if not rate or rate <= 0:
            raise ValueError()

        rate_val = str(rate)
        data = await state.get_data()
        history = push_wizard_step(data.get("step_history", []), "rate", {"rate": rate_val})

        await state.update_data(
            rate=rate_val,
            candidate_rate=rate_val,
            step_history=history
        )

        if data.get("return_to_summary"):
            await state.update_data(return_to_summary=False)
            await _show_wizard_summary(message, state)
            return

        rate_str = format_syp(rate)
        await message.answer(
            f"📝 **إنشاء عرض** | سعر الصرف المعتمد: `{rate_str}` ل.س\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📦 أدخل الحد الأدنى للكمية المسموح بها في الصفقة (USDT):\n"
            f"━━━━━━━━━━━━━━━",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_min"),
            parse_mode="Markdown"
        )
        await state.set_state(OfferCreateFSM.waiting_for_min_limit)
    except Exception:
        await message.answer(
            "⚠️ **سعر الصرف غير صالح!**\n\n"
            "يرجى إدخال رقم صحيح وموجب لسعر الصرف:",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_rate"),
            parse_mode="Markdown"
        )


@offers_router.callback_query(OfferCreateFSM.confirm_rate, F.data == "wiz_edit_rate")
async def edit_rate_input(callback: CallbackQuery, state: FSMContext):
    """Prompt user to re-enter exchange rate."""
    data = await state.get_data()
    offer_type_ar = "🔴 بيع USDT" if data.get("offer_type") == "sell" else "🟢 شراء USDT"
    await callback.message.edit_text(
        f"📝 **تعديل السعر — {offer_type_ar}**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💵 أدخل سعر الصرف الجديد بالليرة السورية مقابل 1 USDT:\n"
        f"━━━━━━━━━━━━━━━",
        reply_markup=get_step_nav_kb(back_cb="wiz_back_rate"),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_rate)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.confirm_rate, F.data == "wiz_confirm_rate")
async def confirm_rate_and_advance(callback: CallbackQuery, state: FSMContext):
    """Commit rate and proceed to Min Limit or return to Summary if jump-editing."""
    data = await state.get_data()
    rate_val = data.get("candidate_rate") or data.get("rate")
    history = push_wizard_step(data.get("step_history", []), "rate", {"rate": rate_val})

    await state.update_data(
        rate=rate_val,
        step_history=history
    )

    if data.get("return_to_summary"):
        await state.update_data(return_to_summary=False)
        await _show_wizard_summary(callback.message, state)
        await callback.answer("تم تحديث السعر.")
        return

    rate_str = format_syp(Decimal(str(rate_val)))
    await callback.message.edit_text(
        f"📝 **إنشاء عرض** | سعر الصرف المعتمد: `{rate_str}` ل.س\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📦 أدخل الحد الأدنى للكمية المسموح بها في الصفقة (USDT):\n"
        f"━━━━━━━━━━━━━━━",
        reply_markup=get_step_nav_kb(back_cb="wiz_back_min"),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_min_limit)
    await callback.answer()


# ==============================================================================
# STEP 3: MIN LIMIT & CONFIRMATION
# ==============================================================================

@offers_router.callback_query(OfferCreateFSM.waiting_for_min_limit, F.data == "wiz_back_min")
async def back_to_rate(callback: CallbackQuery, state: FSMContext):
    """Navigate back from Min Limit to Rate input."""
    data = await state.get_data()
    offer_type_ar = "🔴 بيع USDT" if data.get("offer_type") == "sell" else "🟢 شراء USDT"
    await callback.message.edit_text(
        f"📝 **إنشاء عرض جديد — {offer_type_ar}**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"💵 أدخل سعر الصرف بالليرة السورية مقابل 1 USDT:\n"
        f"━━━━━━━━━━━━━━━",
        reply_markup=get_step_nav_kb(back_cb="wiz_back_rate"),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_rate)
    await callback.answer()


@offers_router.message(OfferCreateFSM.waiting_for_min_limit)
async def process_min_limit(message: Message, state: FSMContext):
    """Receive and validate min limit, then proceed directly to max limit or summary."""
    try:
        min_limit = clean_numeric_input(message.text)
        if not min_limit or min_limit <= 0:
            raise ValueError()

        min_val = str(min_limit)
        data = await state.get_data()
        history = push_wizard_step(data.get("step_history", []), "min_limit", {"min_limit": min_val})

        await state.update_data(
            min_limit=min_val,
            candidate_min_limit=min_val,
            step_history=history
        )

        if data.get("return_to_summary"):
            max_val = data.get("max_limit")
            if max_val and Decimal(str(max_val)) < Decimal(str(min_val)):
                await state.update_data(return_to_summary=False)
            else:
                await state.update_data(return_to_summary=False)
                await _show_wizard_summary(message, state)
                return

        min_str = format_usdt(min_limit)
        await message.answer(
            f"📝 **إنشاء عرض** | الحد الأدنى: 📦 `{min_str} USDT`\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"📦 أدخل الحد الأقصى للكمية الإجمالية المعروضة:\n"
            f"*(يجب أن يكون أكبر من أو يساوي `{min_str}` USDT)*\n"
            f"━━━━━━━━━━━━━━━",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_max"),
            parse_mode="Markdown"
        )
        await state.set_state(OfferCreateFSM.waiting_for_max_limit)
    except Exception:
        await message.answer(
            "⚠️ **قيمة غير صالحة!**\n\n"
            "يرجى إدخال رقم موجب وصحيح بوحدة USDT:",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_min"),
            parse_mode="Markdown"
        )


@offers_router.callback_query(OfferCreateFSM.confirm_min_limit, F.data == "wiz_edit_min")
async def edit_min_limit_input(callback: CallbackQuery, state: FSMContext):
    """Prompt user to re-enter min limit."""
    await callback.message.edit_text(
        "📝 **تعديل الحد الأدنى**\n\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 أدخل الحد الأدنى للكمية المسموح بها في الصفقة (USDT):\n"
        "━━━━━━━━━━━━━━━",
        reply_markup=get_step_nav_kb(back_cb="wiz_back_min"),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_min_limit)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.confirm_min_limit, F.data == "wiz_confirm_min")
async def confirm_min_limit_and_advance(callback: CallbackQuery, state: FSMContext):
    """Commit min limit and advance to Max Limit or return to Summary if jump-editing."""
    data = await state.get_data()
    min_val = data.get("candidate_min_limit") or data.get("min_limit")
    history = push_wizard_step(data.get("step_history", []), "min_limit", {"min_limit": min_val})

    await state.update_data(
        min_limit=min_val,
        step_history=history
    )

    if data.get("return_to_summary"):
        max_val = data.get("max_limit")
        if max_val and Decimal(str(max_val)) < Decimal(str(min_val)):
            await state.update_data(return_to_summary=False)
        else:
            await state.update_data(return_to_summary=False)
            await _show_wizard_summary(callback.message, state)
            await callback.answer("تم تحديث الحد الأدنى.")
            return

    min_str = format_usdt(Decimal(str(min_val)))
    await callback.message.edit_text(
        f"📝 **إنشاء عرض** | الحد الأدنى: 📦 `{min_str} USDT`\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📦 أدخل الحد الأقصى للكمية الإجمالية المعروضة:\n"
        f"*(يجب أن يكون أكبر من أو يساوي `{min_str}` USDT)*\n"
        f"━━━━━━━━━━━━━━━",
        reply_markup=get_step_nav_kb(back_cb="wiz_back_max"),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_max_limit)
    await callback.answer()


# ==============================================================================
# STEP 4: MAX LIMIT & CONFIRMATION
# ==============================================================================

@offers_router.callback_query(OfferCreateFSM.waiting_for_max_limit, F.data == "wiz_back_max")
async def back_to_min_limit(callback: CallbackQuery, state: FSMContext):
    """Navigate back from Max Limit to Min Limit input."""
    await callback.message.edit_text(
        "📝 **إنشاء عرض**\n\n"
        "━━━━━━━━━━━━━━━\n"
        "📦 أدخل الحد الأدنى للكمية المسموح بها في الصفقة (USDT):\n"
        "━━━━━━━━━━━━━━━",
        reply_markup=get_step_nav_kb(back_cb="wiz_back_min"),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_min_limit)
    await callback.answer()


@offers_router.message(OfferCreateFSM.waiting_for_max_limit)
async def process_max_limit(message: Message, state: FSMContext):
    """Receive and validate max limit, then proceed directly to payment methods or summary."""
    try:
        max_limit = clean_numeric_input(message.text)
        if not max_limit or max_limit <= 0:
            raise ValueError()

        data = await state.get_data()
        min_limit = Decimal(str(data.get("min_limit", "0")))

        if max_limit < min_limit:
            await message.answer(
                f"⚠️ **الحد الأقصى غير متطابق!**\n\n"
                f"يجب أن يكون أكبر من أو يساوي الحد الأدنى (`{format_usdt(min_limit)}` USDT).\n"
                f"يرجى إعادة إدخال الحد الأقصى:",
                reply_markup=get_step_nav_kb(back_cb="wiz_back_max"),
                parse_mode="Markdown"
            )
            return

        max_val = str(max_limit)
        history = push_wizard_step(data.get("step_history", []), "max_limit", {"max_limit": max_val})

        await state.update_data(
            max_limit=max_val,
            candidate_max_limit=max_val,
            step_history=history
        )

        if data.get("return_to_summary"):
            await state.update_data(return_to_summary=False)
            await _show_wizard_summary(message, state)
            return

        selected = data.get("payment_methods", [])
        await message.answer(
            "💳 **وسائل الدفع المقبولة**\n\n"
            "━━━━━━━━━━━━━━━\n"
            "اضغط على وسائل الدفع المقبولة لديك لتحديدها (يمكنك اختيار أكثر من وسيلة)، ثم اضغط على زر التأكيد:\n"
            "━━━━━━━━━━━━━━━",
            reply_markup=get_payment_methods_kb(selected),
            parse_mode="Markdown"
        )
        await state.set_state(OfferCreateFSM.waiting_for_methods)
    except Exception:
        await message.answer(
            "⚠️ **قيمة غير صالحة!**\n\n"
            "يرجى إدخال رقم صحيح وموجب للحد الأقصى بوحدة USDT:",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_max"),
            parse_mode="Markdown"
        )



@offers_router.callback_query(OfferCreateFSM.confirm_max_limit, F.data == "wiz_edit_max")
async def edit_max_limit_input(callback: CallbackQuery, state: FSMContext):
    """Prompt user to re-enter max limit."""
    data = await state.get_data()
    min_str = format_usdt(Decimal(str(data.get("min_limit", "0"))))
    await callback.message.edit_text(
        f"📝 **تعديل الحد الأقصى** | الحد الأدنى: 📦 `{min_str} USDT`\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📦 أدخل الحد الأقصى الجديد للكمية بوحدة USDT:\n"
        f"━━━━━━━━━━━━━━━",
        reply_markup=get_step_nav_kb(back_cb="wiz_back_max"),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_max_limit)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.confirm_max_limit, F.data == "wiz_confirm_max")
async def confirm_max_limit_and_advance(callback: CallbackQuery, state: FSMContext):
    """Commit max limit and advance to Payment Methods or return to Summary if jump-editing."""
    data = await state.get_data()
    max_val = data.get("candidate_max_limit") or data.get("max_limit")
    history = push_wizard_step(data.get("step_history", []), "max_limit", {"max_limit": max_val})

    await state.update_data(
        max_limit=max_val,
        step_history=history
    )

    if data.get("return_to_summary"):
        await state.update_data(return_to_summary=False)
        await _show_wizard_summary(callback.message, state)
        await callback.answer("تم تحديث الحد الأقصى.")
        return

    selected_methods = data.get("payment_methods", [])
    kb = get_payment_methods_kb(selected_methods)
    await callback.message.edit_text(
        "💳 **وسائل الدفع المقبولة**\n\n"
        "━━━━━━━━━━━━━━━\n"
        "اضغط على وسائل الدفع المقبولة لديك لتحديدها (يمكنك اختيار أكثر من وسيلة)، ثم اضغط على زر التأكيد:\n"
        "━━━━━━━━━━━━━━━",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_methods)
    await callback.answer()


# ==============================================================================
# STEP 5: PAYMENT METHODS MULTI-SELECT
# ==============================================================================

@offers_router.callback_query(OfferCreateFSM.waiting_for_methods, F.data == "wiz_back_methods")
async def back_to_max_limit(callback: CallbackQuery, state: FSMContext):
    """Navigate back from Payment Methods directly to Max Limit input."""
    await edit_max_limit_input(callback, state)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_methods, F.data.startswith("pm_toggle_"))
async def toggle_payment_method(callback: CallbackQuery, state: FSMContext):
    """Toggle payment method selection in multi-select keyboard."""
    method = callback.data.split("pm_toggle_")[1]
    data = await state.get_data()
    selected_methods = list(data.get("payment_methods", []))

    if method in selected_methods:
        selected_methods.remove(method)
    else:
        selected_methods.append(method)

    await state.update_data(payment_methods=selected_methods)
    kb = get_payment_methods_kb(selected_methods)
    await callback.message.edit_reply_markup(reply_markup=kb)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_methods, F.data == "pm_done")
async def confirm_payment_methods(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    """Validate selected methods and proceed to Step 6 (Payout Details)."""
    data = await state.get_data()
    selected_methods = data.get("payment_methods", [])

    if not selected_methods:
        await callback.answer("⚠️ يرجى اختيار وسيلة دفع واحدة على الأقل للمتابعة.", show_alert=True)
        return

    history = push_wizard_step(data.get("step_history", []), "methods", {"payment_methods": selected_methods})
    await state.update_data(step_history=history)

    # Initialize Step 6 Payout Collection
    offer_type = data.get("offer_type", "sell")
    if offer_type == "sell":
        # Clear out accounts for methods no longer selected
        existing_payouts = dict(data.get("payout_details", {}) or {})
        cleaned_payouts = {k: v for k, v in existing_payouts.items() if k in selected_methods}
        await state.update_data(
            payout_details=cleaned_payouts,
            pending_payout_methods=list(selected_methods),
            current_payout_index=0
        )
    else:
        # Buy offer: single BEP-20 address
        await state.update_data(
            payout_details=data.get("payout_details", {}) or {},
            pending_payout_methods=[]
        )

    await _show_payout_prompt(callback.message, state, db_session, user_id=callback.from_user.id)
    await callback.answer()


# ==============================================================================
# STEP 6: PAYOUT DETAILS COLLECTION & CONFIRMATION
# ==============================================================================

def _extract_saved_payment_info(user_obj: User | None, cur_method: str) -> str | None:
    """Helper to extract saved payment method info from User.saved_payment_info whether dict, JSON, or string."""
    if not user_obj or not user_obj.saved_payment_info:
        return None
    raw = user_obj.saved_payment_info
    if isinstance(raw, dict):
        val = raw.get(cur_method)
        if val:
            return str(val)
    elif isinstance(raw, str):
        raw_str = raw.strip()
        try:
            parsed = json.loads(raw_str)
            if isinstance(parsed, dict) and parsed.get(cur_method):
                return str(parsed[cur_method])
        except Exception:
            pass
        # Check if the plain string is valid for this method or contains the method name
        is_val, norm_info, _ = validate_payment_method_info(cur_method, raw_str)
        if is_val:
            return norm_info
        if cur_method in raw_str:
            return raw_str
    return None


async def _show_payout_prompt(message: Message, state: FSMContext, db_session: AsyncSession, user_id: int):
    """Render prompt for payout details according to offer type and remaining methods."""
    data = await state.get_data()
    offer_type = data.get("offer_type", "sell")

    # Fetch user for saved payment info
    user_res = await db_session.execute(select(User).where(User.telegram_id == user_id))
    user = user_res.scalar_one_or_none()

    if offer_type == "buy":
        # USDT BEP-20 wallet address collection
        buttons = []
        saved_addr = user.saved_usdt_address if (user and user.saved_usdt_address) else None
        if saved_addr:
            short_addr = f"{saved_addr[:6]}...{saved_addr[-4:]}"
            buttons.append([InlineKeyboardButton(text=f"⚡ استخدام عنوان المحفظة المحفوظ ({short_addr})", callback_data="wiz_use_saved_bep20")])

        buttons.append([
            InlineKeyboardButton(text="🔙 رجوع", callback_data="wiz_back_payout"),
            InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")
        ])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        text = (
            "💼 **عنوان محفظة استلام USDT**\n\n"
            "━━━━━━━━━━━━━━━\n"
            "⚠️ **تنبيه شبكة الاستلام:** يجب أن يكون العنوان على شبكة **BNB Smart Chain (BEP-20)** حصراً.\n"
            "إرسال عنوان من شبكة أخرى قد تؤدي إلى فقدان أموالك نهائياً.\n"
            "━━━━━━━━━━━━━━━\n\n"
            "أدخل عنوان محفظتك لاستلام أرباحك عند اكتمال الصفقات:"
        )
        if isinstance(message, CallbackQuery):
            await message.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        else:
            try:
                await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
            except Exception:
                await message.answer(text, reply_markup=kb, parse_mode="Markdown")
        await state.set_state(OfferCreateFSM.waiting_for_payout_details)

    else:
        # Sell offer: sequential collection for each selected method
        methods = data.get("pending_payout_methods") or data.get("payment_methods", [])
        idx = data.get("current_payout_index", 0)

        if idx >= len(methods):
            # All methods gathered, show confirmation screen
            await _show_payout_confirmation(message, state)
            return

        cur_method = methods[idx]
        buttons = []

        # Check for saved info matching this method
        saved_info_str = _extract_saved_payment_info(user, cur_method)

        if saved_info_str:
            buttons.append([InlineKeyboardButton(text=f"⚡ استخدام الحساب المحفوظ لـ {cur_method}", callback_data=f"wiz_use_saved_pm_{idx}")])

        buttons.append([
            InlineKeyboardButton(text="🔙 رجوع", callback_data="wiz_back_payout"),
            InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")
        ])
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)

        icon = PAYMENT_METHOD_ICONS.get(cur_method, "💳")
        step_str = f"({idx + 1}/{len(methods)})"

        text = (
            f"💳 **بيانات استلام: {icon} {cur_method}** {step_str}\n\n"
            f"━━━━━━━━━━━━━━━\n"
            f"أدخل بيانات حسابك المعتمد لاستلام المبالغ بالليرة السورية عبر **{cur_method}**:\n"
            f"━━━━━━━━━━━━━━━"
        )
        if isinstance(message, CallbackQuery):
            await message.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        else:
            try:
                await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
            except Exception:
                await message.answer(text, reply_markup=kb, parse_mode="Markdown")
        await state.set_state(OfferCreateFSM.waiting_for_payout_details)


@offers_router.callback_query(OfferCreateFSM.waiting_for_payout_details, F.data == "wiz_use_saved_bep20")
async def use_saved_bep20_address(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    """Use user's saved BEP20 address."""
    user_res = await db_session.execute(select(User).where(User.telegram_id == callback.from_user.id))
    user = user_res.scalar_one_or_none()
    if not user or not user.saved_usdt_address:
        await callback.answer("لا يوجد عنوان محفوظ في ملفك الشخصي.", show_alert=True)
        return

    is_valid, norm_addr, _ = validate_bep20_address(user.saved_usdt_address)
    if not is_valid:
        await callback.answer("العنوان المحفوظ غير صالح.", show_alert=True)
        return

    data = await state.get_data()
    payout_details = dict(data.get("payout_details", {}) or {})
    payout_details["usdt_bep20_address"] = norm_addr
    await state.update_data(payout_details=payout_details)

    await _show_payout_confirmation(callback.message, state)
    await callback.answer("تم استخدام العنوان المحفوظ.")


@offers_router.callback_query(OfferCreateFSM.waiting_for_payout_details, F.data.startswith("wiz_use_saved_pm_"))
async def use_saved_payment_method_info(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    """Use user's saved account info for the current payment method."""
    user_res = await db_session.execute(select(User).where(User.telegram_id == callback.from_user.id))
    user = user_res.scalar_one_or_none()
    data = await state.get_data()
    methods = data.get("pending_payout_methods") or data.get("payment_methods", [])
    idx = data.get("current_payout_index", 0)

    if idx >= len(methods) or not user or not user.saved_payment_info:
        await callback.answer("لا توجد بيانات محفوظة لهذه الوسيلة.", show_alert=True)
        return

    cur_method = methods[idx]
    saved_val = _extract_saved_payment_info(user, cur_method)

    if not saved_val:
        await callback.answer("لا توجد بيانات محفوظة لهذه الوسيلة.", show_alert=True)
        return

    is_valid, norm_info, err = validate_payment_method_info(cur_method, saved_val)
    if not is_valid:
        await callback.answer("البيانات المحفوظة غير متوافقة.", show_alert=True)
        return

    payout_details = dict(data.get("payout_details", {}) or {})
    payout_details[cur_method] = norm_info
    next_idx = idx + 1
    await state.update_data(
        payout_details=payout_details,
        current_payout_index=next_idx
    )

    if next_idx >= len(methods):
        await _show_payout_confirmation(callback.message, state)
    else:
        await _show_payout_prompt(callback.message, state, db_session, user_id=callback.from_user.id)
    await callback.answer("تم اعتماد الحساب.")



@offers_router.message(OfferCreateFSM.waiting_for_payout_details)
async def process_payout_text_input(message: Message, state: FSMContext, db_session: AsyncSession):
    """Receive and validate user typed payout information."""
    data = await state.get_data()
    offer_type = data.get("offer_type", "sell")

    if offer_type == "buy":
        is_valid, norm_addr, err = validate_bep20_address(message.text)
        if not is_valid:
            await message.answer(
                f"{err}\n\nيرجى إعادة إرسال عنوان المحفظة بشكل صحيح:",
                reply_markup=get_step_nav_kb(back_cb="wiz_back_payout"),
                parse_mode="Markdown"
            )
            return

        payout_details = dict(data.get("payout_details", {}) or {})
        payout_details["usdt_bep20_address"] = norm_addr
        await state.update_data(payout_details=payout_details)
        await _show_payout_confirmation(message, state)

    else:
        methods = data.get("pending_payout_methods") or data.get("payment_methods", [])
        idx = data.get("current_payout_index", 0)
        if idx >= len(methods):
            await _show_payout_confirmation(message, state)
            return

        cur_method = methods[idx]
        is_valid, norm_info, err = validate_payment_method_info(cur_method, message.text)
        if not is_valid:
            await message.answer(
                f"{err}\n\nيرجى إعادة إدخال بيانات **{cur_method}**:",
                reply_markup=get_step_nav_kb(back_cb="wiz_back_payout"),
                parse_mode="Markdown"
            )
            return

        payout_details = dict(data.get("payout_details", {}) or {})
        payout_details[cur_method] = norm_info
        next_idx = idx + 1
        await state.update_data(
            payout_details=payout_details,
            current_payout_index=next_idx
        )

        if next_idx >= len(methods):
            await _show_payout_confirmation(message, state)
        else:
            await _show_payout_prompt(message, state, db_session, user_id=message.from_user.id)


async def _show_payout_confirmation(message: Message, state: FSMContext):
    """Display gathered payout details for owner confirmation."""
    data = await state.get_data()
    offer_type = data.get("offer_type", "sell")
    payout_details = data.get("payout_details", {}) or {}

    lines = []
    if offer_type == "buy":
        wallet = payout_details.get("usdt_bep20_address", "")
        lines.append(f"• **محفظة استلام USDT (BEP-20):**\n  `{wallet}`")
    else:
        methods = data.get("payment_methods", [])
        for m in methods:
            acc = payout_details.get(m, "غير محدد")
            icon = PAYMENT_METHOD_ICONS.get(m, "💳")
            lines.append(f"• **{icon} {m}:**\n  `{acc}`")

    details_text = "\n\n".join(lines) if lines else "لا توجد بيانات مسجلة."

    kb = get_confirm_step_kb(
        confirm_cb="wiz_confirm_payout",
        edit_cb="wiz_edit_payout",
        back_cb="wiz_back_payout"
    )
    text = (
        "💼 **تأكيد بيانات الاستلام الخاصة بك**\n\n"
        "━━━━━━━━━━━━━━━\n"
        f"{details_text}\n"
        "━━━━━━━━━━━━━━━\n"
        "🔒 *ملاحظة:* هذه البيانات تظهر فقط لأطراف الصفقات التنفيذية ولا تُنشر في القناة العامة.\n\n"
        "هل ترغب باعتماد هذه البيانات والمتابعة؟"
    )
    if isinstance(message, CallbackQuery):
        await message.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        try:
            await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await state.set_state(OfferCreateFSM.confirm_payout_details)


@offers_router.callback_query(F.data == "wiz_back_payout")
async def back_from_payout(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    """Navigate back from payout step to payment methods or previous payout index."""
    data = await state.get_data()
    offer_type = data.get("offer_type", "sell")
    cur_state = await state.get_state()

    if cur_state == OfferCreateFSM.confirm_payout_details.state:
        # Return to editing payout details
        await edit_payout_details(callback, state, db_session)
        return

    if offer_type == "sell":
        idx = data.get("current_payout_index", 0)
        if idx > 0:
            # Step back one payment method
            await state.update_data(current_payout_index=idx - 1)
            await _show_payout_prompt(callback.message, state, db_session, user_id=callback.from_user.id)
            await callback.answer()
            return

    # Otherwise return to payment methods selection
    selected = data.get("payment_methods", [])
    await callback.message.edit_text(
        "💳 **وسائل الدفع المقبولة**\n\n"
        "━━━━━━━━━━━━━━━\n"
        "اضغط على وسائل الدفع المقبولة لديك لتحديدها (يمكنك اختيار أكثر من وسيلة)، ثم اضغط على زر التأكيد:\n"
        "━━━━━━━━━━━━━━━",
        reply_markup=get_payment_methods_kb(selected),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_methods)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.confirm_payout_details, F.data == "wiz_edit_payout")
async def edit_payout_details(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    """Restart payout details collection for this draft."""
    data = await state.get_data()
    methods = data.get("payment_methods", [])
    await state.update_data(
        payout_details={},
        pending_payout_methods=list(methods),
        current_payout_index=0
    )
    await _show_payout_prompt(callback.message, state, db_session, user_id=callback.from_user.id)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.confirm_payout_details, F.data == "wiz_confirm_payout")
async def confirm_payout_and_advance(callback: CallbackQuery, state: FSMContext):
    """Commit payout details and proceed to Negotiation decision or return to Summary."""
    data = await state.get_data()
    payout_details = data.get("payout_details", {})
    history = push_wizard_step(data.get("step_history", []), "payout_details", {"payout_details": payout_details})
    await state.update_data(step_history=history)

    if data.get("return_to_summary"):
        await state.update_data(return_to_summary=False)
        await _show_wizard_summary(callback.message, state)
        await callback.answer("تم تحديث بيانات الاستلام.")
        return

    await _show_negotiation_decision(callback.message)
    await state.set_state(OfferCreateFSM.waiting_for_negotiable_decision)
    await callback.answer()


# ==============================================================================
# STEP 7: NEGOTIATION OPTIONS
# ==============================================================================

async def _show_negotiation_decision(message: Message):
    """Render negotiation choices with Back and Cancel buttons."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤝 إتاحة التفاوض", callback_data="neg_choice_yes"),
            InlineKeyboardButton(text="🔒 شروط ثابتة فقط", callback_data="neg_choice_no")
        ],
        [
            InlineKeyboardButton(text="🔙 رجوع", callback_data="wiz_back_negotiation"),
            InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")
        ]
    ])
    text = (
        "⚙️ **خيارات التفاوض على العرض**\n\n"
        "━━━━━━━━━━━━━━━\n"
        "هل ترغب بالسماح للمتداولين بتقديم اقتراحات تفاوض على السعر أو الكمية؟\n"
        "━━━━━━━━━━━━━━━\n\n"
        "اختر الإعداد المناسب:"
    )
    if hasattr(message, "edit_text"):
        await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@offers_router.callback_query(F.data == "wiz_back_negotiation")
async def back_to_payout_confirmation(callback: CallbackQuery, state: FSMContext):
    """Navigate back from Negotiation to Payout confirmation."""
    await _show_payout_confirmation(callback.message, state)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_negotiable_decision, F.data.startswith("neg_choice_"))
async def process_negotiable_decision(callback: CallbackQuery, state: FSMContext):
    """Handle choice between open negotiation and fixed terms."""
    choice = callback.data.split("_")[-1]
    if choice == "yes":
        await state.update_data(negotiable=True)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📦 على الكمية فقط", callback_data="neg_type_amount")],
            [InlineKeyboardButton(text="💵 على السعر فقط", callback_data="neg_type_rate")],
            [InlineKeyboardButton(text="⚖️ على السعر والكمية معاً", callback_data="neg_type_both")],
            [
                InlineKeyboardButton(text="🔙 رجوع", callback_data="wiz_back_negotiation_choice"),
                InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")
            ]
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
        data = await state.get_data()
        history = push_wizard_step(
            data.get("step_history", []),
            "negotiation",
            {"negotiable": False, "negotiable_type": "none"}
        )
        await state.update_data(
            negotiable=False,
            negotiable_type="none",
            step_history=history
        )
        if data.get("return_to_summary"):
            await state.update_data(return_to_summary=False)
            await _show_wizard_summary(callback.message, state)
        else:
            await _show_duration_choice(callback.message)
            await state.set_state(OfferCreateFSM.waiting_for_duration_choice)
    await callback.answer()


@offers_router.callback_query(F.data == "wiz_back_negotiation_choice")
async def back_to_negotiation_decision(callback: CallbackQuery, state: FSMContext):
    """Navigate back to negotiation yes/no choice."""
    await _show_negotiation_decision(callback.message)
    await state.set_state(OfferCreateFSM.waiting_for_negotiable_decision)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_negotiable_type, F.data.startswith("neg_type_"))
async def process_negotiable_type(callback: CallbackQuery, state: FSMContext):
    """Capture selected negotiation scope and advance."""
    neg_type = callback.data.replace("neg_type_", "")
    data = await state.get_data()
    history = push_wizard_step(
        data.get("step_history", []),
        "negotiation",
        {"negotiable": True, "negotiable_type": neg_type}
    )
    await state.update_data(
        negotiable=True,
        negotiable_type=neg_type,
        step_history=history
    )
    if data.get("return_to_summary"):
        await state.update_data(return_to_summary=False)
        await _show_wizard_summary(callback.message, state)
    else:
        await _show_duration_choice(callback.message)
        await state.set_state(OfferCreateFSM.waiting_for_duration_choice)
    await callback.answer()


# ==============================================================================
# STEP 8: OFFER DURATION & CONFIRMATION
# ==============================================================================

async def _show_duration_choice(message: Message):
    """Render duration options with Back and Cancel buttons."""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱️ 4 ساعات (الافتراضي)", callback_data="dur_default_4")],
        [InlineKeyboardButton(text="⚙️ تحديد مدة مخصصة (1-4 ساعات)", callback_data="dur_custom")],
        [
            InlineKeyboardButton(text="🔙 رجوع", callback_data="wiz_back_duration"),
            InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")
        ]
    ])
    text = (
        "⏱️ **مدة صلاحية العرض في السوق**\n\n"
        "━━━━━━━━━━━━━━━\n"
        "اختر مدة بقاء عرضك متاحاً ونشطاً في السوق قبل انتهاء صلاحيته آلياً:\n"
        "━━━━━━━━━━━━━━━"
    )
    if hasattr(message, "edit_text"):
        await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@offers_router.callback_query(F.data == "wiz_back_duration")
async def back_to_negotiation_step(callback: CallbackQuery, state: FSMContext):
    """Navigate back from Duration to Negotiation choices."""
    await _show_negotiation_decision(callback.message)
    await state.set_state(OfferCreateFSM.waiting_for_negotiable_decision)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_duration_choice, F.data == "dur_default_4")
async def process_duration_default(callback: CallbackQuery, state: FSMContext):
    """Set default 4 hours duration and show summary."""
    data = await state.get_data()
    history = push_wizard_step(data.get("step_history", []), "duration", {"duration_hours": 4.0})
    await state.update_data(duration_hours=4.0, step_history=history)
    await _show_wizard_summary(callback.message, state)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_duration_choice, F.data == "dur_custom")
async def prompt_custom_duration(callback: CallbackQuery, state: FSMContext):
    """Prompt user for custom duration input."""
    await callback.message.edit_text(
        "⏱️ **تحديد مدة العرض المخصصة**\n\n"
        "━━━━━━━━━━━━━━━\n"
        "أدخل عدد الساعات المطلوب لبقاء العرض نشطاً (رقم من 1 إلى 4):\n"
        "━━━━━━━━━━━━━━━",
        reply_markup=get_step_nav_kb(back_cb="wiz_back_custom_dur"),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_custom_duration)
    await callback.answer()


@offers_router.callback_query(F.data == "wiz_back_custom_dur")
async def back_to_duration_choice(callback: CallbackQuery, state: FSMContext):
    """Navigate back from custom duration input to duration choice keyboard."""
    await _show_duration_choice(callback.message)
    await state.set_state(OfferCreateFSM.waiting_for_duration_choice)
    await callback.answer()


@offers_router.message(OfferCreateFSM.waiting_for_custom_duration)
async def process_custom_duration(message: Message, state: FSMContext):
    """Receive custom duration and show confirmation."""
    try:
        hours_val = float(message.text.strip())
        if hours_val < 1.0 or hours_val > 4.0:
            raise ValueError()

        await state.update_data(candidate_duration=hours_val)
        dur_str = f"{int(hours_val)} ساعات" if hours_val == int(hours_val) else f"{hours_val} ساعة"
        kb = get_confirm_step_kb(
            confirm_cb="wiz_confirm_dur",
            edit_cb="wiz_edit_dur",
            back_cb="wiz_back_custom_dur"
        )
        await message.answer(
            "⏱️ **تأكيد مدة الصلاحية**\n\n"
            "━━━━━━━━━━━━━━━\n"
            f"• مدة بقاء العرض في السوق: `{dur_str}`\n"
            "━━━━━━━━━━━━━━━\n\n"
            "هل ترغب باعتماد هذه المدة والمتابعة إلى الملخص النهائي؟",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        await state.set_state(OfferCreateFSM.confirm_custom_duration)
    except Exception:
        await message.answer(
            "⚠️ **قيمة غير صالحة!**\n\n"
            "يرجى إرسال رقم صحيح لعدد الساعات (بين 1 و 4):",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_custom_dur"),
            parse_mode="Markdown"
        )


@offers_router.callback_query(OfferCreateFSM.confirm_custom_duration, F.data == "wiz_edit_dur")
async def edit_custom_duration(callback: CallbackQuery, state: FSMContext):
    """Prompt user to re-enter custom duration."""
    await prompt_custom_duration(callback, state)


@offers_router.callback_query(OfferCreateFSM.confirm_custom_duration, F.data == "wiz_confirm_dur")
async def confirm_custom_duration_and_advance(callback: CallbackQuery, state: FSMContext):
    """Commit custom duration and advance to Summary."""
    data = await state.get_data()
    dur_val = data.get("candidate_duration", 4.0)
    history = push_wizard_step(data.get("step_history", []), "duration", {"duration_hours": dur_val})
    await state.update_data(duration_hours=dur_val, step_history=history)
    await _show_wizard_summary(callback.message, state)
    await callback.answer()


# ==============================================================================
# STEP 9: FINAL SUMMARY SCREEN & JUMP-EDIT HANDLERS
# ==============================================================================

async def _show_wizard_summary(message: Message, state: FSMContext):
    """Render full wizard summary with jump-edit options."""
    data = await state.get_data()
    summary_text = format_wizard_summary(data, bot_name=BOT_NAME)
    kb = get_wizard_summary_kb()

    if hasattr(message, "edit_text"):
        await message.edit_text(summary_text, reply_markup=kb, parse_mode="Markdown")
    else:
        await message.answer(summary_text, reply_markup=kb, parse_mode="Markdown")
    await state.set_state(OfferCreateFSM.waiting_for_confirm_publish)


@offers_router.callback_query(F.data == "wiz_back_summary")
async def back_from_summary(callback: CallbackQuery, state: FSMContext):
    """Navigate back from Summary to Duration choice."""
    await _show_duration_choice(callback.message)
    await state.set_state(OfferCreateFSM.waiting_for_duration_choice)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_confirm_publish, F.data == "wiz_jump_rate")
async def jump_edit_rate(callback: CallbackQuery, state: FSMContext):
    """Jump from summary to edit rate."""
    await state.update_data(return_to_summary=True)
    await edit_rate_input(callback, state)


@offers_router.callback_query(OfferCreateFSM.waiting_for_confirm_publish, F.data == "wiz_jump_limits")
async def jump_edit_limits(callback: CallbackQuery, state: FSMContext):
    """Jump from summary to edit min/max limits."""
    await state.update_data(return_to_summary=True)
    await edit_min_limit_input(callback, state)


@offers_router.callback_query(OfferCreateFSM.waiting_for_confirm_publish, F.data == "wiz_jump_methods")
async def jump_edit_methods(callback: CallbackQuery, state: FSMContext):
    """Jump from summary to edit payment methods."""
    await state.update_data(return_to_summary=True)
    data = await state.get_data()
    selected = data.get("payment_methods", [])
    await callback.message.edit_text(
        "💳 **وسائل الدفع المقبولة**\n\n"
        "━━━━━━━━━━━━━━━\n"
        "اضغط على وسائل الدفع المقبولة لديك لتحديدها (يمكنك اختيار أكثر من وسيلة)، ثم اضغط على زر التأكيد:\n"
        "━━━━━━━━━━━━━━━",
        reply_markup=get_payment_methods_kb(selected),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_methods)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_confirm_publish, F.data == "wiz_jump_payout")
async def jump_edit_payout(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    """Jump from summary to edit payout details."""
    await state.update_data(return_to_summary=True)
    await edit_payout_details(callback, state, db_session)


@offers_router.callback_query(OfferCreateFSM.waiting_for_confirm_publish, F.data == "wiz_jump_neg")
async def jump_edit_negotiation(callback: CallbackQuery, state: FSMContext):
    """Jump from summary to edit negotiation options."""
    await state.update_data(return_to_summary=True)
    await _show_negotiation_decision(callback.message)
    await state.set_state(OfferCreateFSM.waiting_for_negotiable_decision)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_confirm_publish, F.data == "wiz_jump_dur")
async def jump_edit_duration(callback: CallbackQuery, state: FSMContext):
    """Jump from summary to edit offer duration."""
    await state.update_data(return_to_summary=True)
    await _show_duration_choice(callback.message)
    await state.set_state(OfferCreateFSM.waiting_for_duration_choice)
    await callback.answer()


# ==============================================================================
# STEP 10: CONFIRM PUBLISH & DATABASE COMMIT & CHANNEL BROADCAST
# ==============================================================================

@offers_router.callback_query(OfferCreateFSM.waiting_for_confirm_publish, F.data == "wiz_publish")
async def publish_offer_final(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession, bot: Bot):
    """Publish offer after validating completeness, rate limits, and active offer caps."""
    user_id = callback.from_user.id
    data = await state.get_data()

    # Double click gate
    if data.get("is_publishing"):
        await callback.answer("جاري معالجة طلب نشر العرض...", show_alert=True)
        return
    await state.update_data(is_publishing=True)

    # 1. Wizard completeness check
    is_complete, err_msg = is_wizard_data_complete(data)
    if not is_complete:
        await state.update_data(is_publishing=False)
        await callback.answer(f"⚠️ {err_msg}", show_alert=True)
        return

    # 2. Section 6.1.1: Active offers cap check (max 5 active offers for non-admins)
    if user_id not in settings.ADMIN_IDS:
        count_res = await db_session.execute(
            select(func.count(Offer.id)).where(
                Offer.user_id == user_id,
                Offer.status.in_(['active', 'reserved', 'negotiating', 'expired_grace'])
            )
        )
        active_offers_count = count_res.scalar() or 0
        if active_offers_count >= 5:
            await state.update_data(is_publishing=False)
            await callback.answer(
                "⚠️ تجاوزت الحد الأقصى المسموح به (5 عروض نشطة). يرجى إلغاء أو إكمال أحد عروضك الحالية أولاً.",
                show_alert=True
            )
            return

        # 3. Section 6.1.2: Rolling window rate limit (max 3 offers per 10 mins)
        is_allowed, retry_after = check_and_record_rate_limit(user_id, "create_offer")
        if not is_allowed:
            minutes = (retry_after + 59) // 60
            await state.update_data(is_publishing=False)
            await callback.answer(
                f"⚠️ تجاوزت حد إنشاء العروض. يرجى الانتظار حوالي {minutes} دقيقة والمحاولة مجدداً.",
                show_alert=True
            )
            return


    # 4. Extract data and prepare timestamps
    now = datetime.now(timezone.utc)
    duration_hours = float(data.get("duration_hours", 4.0))
    expires_at = now + timedelta(hours=duration_hours)
    primary_payout_str = extract_primary_payout_info(data)
    payout_details_json = data.get("payout_details", {}) or {}

    new_offer = Offer(
        user_id=user_id,
        offer_type=data["offer_type"],
        rate=Decimal(str(data["rate"])),
        min_limit=Decimal(str(data["min_limit"])),
        max_limit=Decimal(str(data["max_limit"])),
        payment_methods=data["payment_methods"],
        payout_info=primary_payout_str,  # Backward compatibility with deals.py
        payout_details=payout_details_json,  # New JSONB storage
        negotiable=data.get("negotiable", False),
        negotiable_type=data.get("negotiable_type", "none"),
        expires_at=expires_at,
        status="active",
        status_changed_at=now
    )
    db_session.add(new_offer)
    await db_session.commit()
    await db_session.refresh(new_offer)

    # 5. Presentation formatters
    duration_str = f"{int(duration_hours)} ساعات" if duration_hours == int(duration_hours) else f"{duration_hours} ساعة"
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

    await callback.message.edit_text(
        f"🎉 **تم نشر العرض بنجاح — عرض #{new_offer.id}** ✅\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• النوع: {offer_type_ar}\n"
        f"▫️ **سعر الصرف :** `{rate_str}` ل.س\n"
        f"• حدود الكمية: 📦 من `{min_str}` إلى `{max_str}` USDT\n"
        f"• وسائل الدفع: 💳 {methods_str}\n"
        f"• حالة التفاوض: {neg_status_ar}\n"
        f"• {FEE_RULE_LINE}\n"
        f"• مدة الصلاحية: ⏱️ {duration_str}\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"تم إدراج العرض بنجاح في سوق {BOT_NAME} وقناة {CHANNEL_NAME}.",
        parse_mode="Markdown"
    )
    await state.clear()
    await callback.answer("تم النشر بنجاح!")

    # 6. Broadcast to Channel (Notice: No payout details in public channel!)
    if settings.CHANNEL_ID and settings.BOT_USERNAME:
        offer_type_chan = "🔴 بيع (المعلن يبيع)" if new_offer.offer_type == 'sell' else "🟢 شراء (المعلن يشتري)"
        action_ar = "💸 تنفيذ هذا العرض" if new_offer.offer_type == 'sell' else "💸 البيع لهذا الزبون"
        neg_line = f"\n▫️ <b>التفاوض :</b> {neg_status_ar}" if new_offer.negotiable else ""

        channel_text = (
            f"📢 <b>{CHANNEL_NAME} | عرض #{new_offer.id} [متاح للتداول]</b>\n\n"
            f"▫️ <b>النوع :</b> {offer_type_chan}\n"
            f"────────────────\n"
            f"▫️ <b>سعر الصرف :</b> {rate_str} ل.س\n"
            f"▫️ <b>حدود الكمية :</b> من {min_str} إلى {max_str} دولار\n"
            f"▫️ <b>طرق الدفع :</b> {methods_str}{neg_line}\n"
            f"▫️ <b>مدة المعاملة :</b> {duration_str}\n"
            f"────────────────\n"
            f"{format_fee_rule_html()}"
        )

        deep_link = f"https://t.me/{settings.BOT_USERNAME}?start=offer_{new_offer.id}"
        kb_chan = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text=action_ar, url=deep_link),
                InlineKeyboardButton(text="🔍 تصفح عروض أخرى", url=f"https://t.me/{settings.BOT_USERNAME}?start=offers")
            ]
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
            logger.warning(f"Failed to broadcast to channel: {e}")


from bot.utils.deal_views import build_offer_action_buttons

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
        buttons = build_offer_action_buttons(
            offer_id=offer.id,
            offer_user_id=offer.user_id,
            viewer_id=message.from_user.id,
            negotiable=offer.negotiable,
            negotiable_type=offer.negotiable_type,
            context="offers"
        )
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
            f"▫️ **سعر الصرف :** `{rate_str}` ل.س\n\n"
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
    offer.status_changed_at = datetime.now(timezone.utc)
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
    offer.status_changed_at = now
    offer.expires_at = now + timedelta(hours=hours)
    await db_session.commit()

    from bot.handlers.deals import update_channel_offer_message
    try:
        await update_channel_offer_message(callback.bot, offer)
    except Exception as e:
        logger.warning(f"Failed to update channel message for extended offer #{offer.id}: {e}")

    expires_str = format_local_datetime(offer.expires_at)
    await callback.answer("تم تمديد صلاحية العرض بنجاح.")
    await callback.message.edit_text(
        f"✅ **عرض #{offer.id} — تم تمديد الصلاحية بنجاح**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• المدة الإضافية:  `{hours}` ساعة\n"
        f"• الصلاحية حتى:  `{expires_str}`\n"
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
