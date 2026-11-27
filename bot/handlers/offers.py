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
from bot.services.channel import update_channel_offer_message, build_channel_offer_message, format_trader_trust_markdown
from bot.services.admin_notifier import dispatch_admin_notification
from bot.utils.formatters import (
    format_syp,
    format_usdt,
    format_payment_methods,
    AVAILABLE_PAYMENT_METHODS,
    PAYMENT_METHOD_ICONS,
    humanize_time_remaining,
    clean_numeric_input,
    format_local_datetime,
    MIN_DEAL_AMOUNT_USDT,
    escape_markdown,
    safe_username,
    styled_button,
)
from bot.utils.offer_wizard import (
    push_wizard_step,
    extract_primary_payout_info,
    is_wizard_data_complete,
    format_wizard_summary,
    format_duration_hours_minutes,
    format_wizard_draft_header,
    SHAM_CASH_NOTE_SUGGESTIONS,
    MAX_OFFER_NOTE_LENGTH,
)
from bot.utils.branding import BOT_NAME
from bot.utils.deal_guard import (
    MAX_ACTIVE_DEALS,
    count_user_active_deals,
    build_max_active_deals_kb,
    build_max_active_deals_notice_text,
)

logger = logging.getLogger(__name__)

offers_router = Router()


# ==============================================================================
# KEYBOARD HELPERS
# ==============================================================================

def get_confirm_step_kb(confirm_cb: str, edit_cb: str, back_cb: str | None = None) -> InlineKeyboardMarkup:
    """Standard confirmation keyboard with Confirm, Edit, Back, and Cancel."""
    rows = [
        [
            InlineKeyboardButton(text="✅ اعتماد", callback_data=confirm_cb),
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
            InlineKeyboardButton(text="✅ نعم، إلغاء", callback_data="wiz_cancel_confirm_yes"),
            InlineKeyboardButton(text="↩️ متابعة", callback_data="wiz_cancel_confirm_no")
        ]
    ])


def get_payment_methods_kb(selected_methods: list[str]) -> InlineKeyboardMarkup:
    """Payment methods multi-select keyboard arranged in a 2x2 grid with Back and Cancel."""
    buttons = []
    row = []
    for method in AVAILABLE_PAYMENT_METHODS:
        mark = "✅ " if method in selected_methods else "⬜ "
        icon = PAYMENT_METHOD_ICONS.get(method, "💳")
        row.append(InlineKeyboardButton(text=f"{mark}{icon} {method}", callback_data=f"pm_toggle_{method}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    buttons.append([InlineKeyboardButton(text="✅ متابعة", callback_data="pm_done")])
    buttons.append([
        InlineKeyboardButton(text="🔙 رجوع", callback_data="wiz_back_methods"),
        InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_wizard_summary_kb() -> InlineKeyboardMarkup:
    """Summary keyboard with Publish, jump-edit buttons, Back, and Cancel."""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 نشر العرض", callback_data="wiz_publish")],
        [
            InlineKeyboardButton(text="✏️ السعر", callback_data="wiz_jump_rate"),
            InlineKeyboardButton(text="✏️ الكمية", callback_data="wiz_jump_limits")
        ],
        [
            InlineKeyboardButton(text="✏️ الدفع", callback_data="wiz_jump_methods"),
            InlineKeyboardButton(text="✏️ التفاوض", callback_data="wiz_jump_neg")
        ],
        [
            InlineKeyboardButton(text="✏️ الملاحظة", callback_data="wiz_jump_note"),
            InlineKeyboardButton(text="✏️ المدة", callback_data="wiz_jump_dur")
        ],
        [
            InlineKeyboardButton(text="🔙 رجوع", callback_data="wiz_back_summary"),
            InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")
        ]
    ])


def build_offer_type_screen(back_to_main: bool = True) -> tuple[str, InlineKeyboardMarkup]:
    """Unified builder for Step 1 (Offer Type selection) across menu button, /create_offer, and Back navigation."""
    text = (
        f"📝 **إنشاء عرض تداول جديد — {BOT_NAME}**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        "اختر نوع العملية التي ترغب بنشرها في السوق:\n\n"
        "🔴 **بيع `USDT (BEP-20)`**\n"
        "▫️ تحدّد سعر الصرف الذي يناسبك\n"
        "▫️ تحدّد أقل وأعلى كمية تقبل بيعها\n"
        "▫️ تحدّد وسيلة استلامك بالليرة السورية\n\n"
        "🟢 **شراء `USDT (BEP-20)`**\n"
        "▫️ تحدّد سعر الشراء الذي يناسبك\n"
        "▫️ تحدّد أقل وأعلى كمية ترغب بشرائها\n"
        "▫️ تحدّد وسائل الدفع المتاحة لديك بالليرة\n\n"
        "👇 **اختر نوع العرض للمتابعة:**"
    )
    footer_btn = (
        InlineKeyboardButton(text="🏠 العودة للقائمة الرئيسية", callback_data="menu_back_main")
        if back_to_main
        else InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [styled_button(text="💵 عرض بيع USDT", callback_data="offer_type_sell", style="danger")],
        [styled_button(text="🛒 عرض شراء USDT", callback_data="offer_type_buy", style="success")],
        [footer_btn]
    ])
    return text, kb


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
        "هل أنت متأكد من رغبتك في إلغاء عملية إنشاء العرض وحذف كافة البيانات المدخلة؟",
        reply_markup=get_cancel_confirm_kb(),
        parse_mode="Markdown"
    )
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.confirm_cancel, F.data == "wiz_cancel_confirm_yes")
async def process_cancel_confirm_yes(callback: CallbackQuery, state: FSMContext):
    """Discard wizard draft data and clear FSM state."""
    await state.clear()
    await callback.message.edit_text(
        "❌ **تم إلغاء عملية إنشاء العرض**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        "يمكنك البدء من جديد عبر `/create_offer` أو من القائمة الرئيسية.",
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
        text, kb = build_offer_type_screen(back_to_main=True)
        await message.edit_text(
            text,
            reply_markup=kb,
            parse_mode="Markdown"
        )
    elif cur_state == OfferCreateFSM.waiting_for_rate.state:
        header = format_wizard_draft_header(data, "rate")
        await message.edit_text(
            f"{header}"
            f"💵 **تحديد سعر الصرف**\n"
            f"أدخل سعر الصرف بالليرة السورية مقابل كل `1 USDT`:",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_rate"),
            parse_mode="Markdown"
        )
    elif cur_state == OfferCreateFSM.waiting_for_min_limit.state:
        header = format_wizard_draft_header(data, "min_limit")
        await message.edit_text(
            f"{header}"
            "📦 **تحديد الحد الأدنى**\n"
            "أدخل أقل كمية تقبل تنفيذها في الصفقة الواحدة (بـ `USDT`):",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_min"),
            parse_mode="Markdown"
        )
    elif cur_state == OfferCreateFSM.waiting_for_max_limit.state:
        header = format_wizard_draft_header(data, "max_limit")
        await message.edit_text(
            f"{header}"
            f"📦 **تحديد الحد الأقصى**\n"
            f"أدخل أعلى كمية إجمالية معروضة للتداول (بـ `USDT`):",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_max"),
            parse_mode="Markdown"
        )
    elif cur_state == OfferCreateFSM.waiting_for_methods.state:
        selected = data.get("payment_methods", [])
        header = format_wizard_draft_header(data, "methods")
        await message.edit_text(
            f"{header}"
            "💳 **وسائل الدفع المقبولة**\n"
            "اختر وسائل الدفع المعتمدة لديك (يمكنك تحديد أكثر من وسيلة)، ثم اضغط على **متابعة**:",
            reply_markup=get_payment_methods_kb(selected),
            parse_mode="Markdown"
        )
    elif cur_state in (OfferCreateFSM.waiting_for_negotiable_decision.state, OfferCreateFSM.waiting_for_negotiable_type.state):
        await _show_negotiation_decision(message, state)
        await state.set_state(OfferCreateFSM.waiting_for_negotiable_decision)
    elif cur_state == OfferCreateFSM.waiting_for_note.state:
        await _show_note_step(message, state)
        await state.set_state(OfferCreateFSM.waiting_for_note)
    elif cur_state in (OfferCreateFSM.waiting_for_duration_choice.state, OfferCreateFSM.waiting_for_custom_duration.state, OfferCreateFSM.confirm_custom_duration.state):
        await _show_duration_choice(message, state)
        await state.set_state(OfferCreateFSM.waiting_for_duration_choice)
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
            "🚫 **تم تقييد حسابك**\n\n"
            f"عذراً، حسابك مقيد حالياً من إنشاء عروض جديدة على {BOT_NAME}.\n"
            "يرجى التواصل مع الدعم الفني لمزيد من التفاصيل.",
            parse_mode="Markdown"
        )
        return

    active_deals_count = await count_user_active_deals(db_session, message.from_user.id)
    if active_deals_count >= MAX_ACTIVE_DEALS:
        await state.clear()
        await message.answer(
            build_max_active_deals_notice_text(),
            reply_markup=build_max_active_deals_kb(),
            parse_mode="Markdown",
        )
        return

    await state.clear()
    await state.update_data(
        step_history=[],
        payout_details={},
        payment_methods=[]
    )

    text, kb = build_offer_type_screen(back_to_main=True)
    await message.answer(
        text,
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_type)


@offers_router.callback_query(OfferCreateFSM.waiting_for_type, F.data.startswith("offer_type_"))
async def process_offer_type(callback: CallbackQuery, state: FSMContext):
    """Capture offer type and advance to rate input."""
    offer_type = callback.data.split("_")[-1]
    data = await state.get_data()
    history = push_wizard_step(data.get("step_history", []), "type", {"offer_type": offer_type})

    await state.update_data(
        offer_type=offer_type,
        step_history=history
    )
    updated_data = await state.get_data()
    header = format_wizard_draft_header(updated_data, "rate")
    await callback.message.edit_text(
        f"{header}"
        f"💵 **تحديد سعر الصرف**\n"
        f"أدخل سعر الصرف بالليرة السورية مقابل كل `1 USDT`:",
        reply_markup=get_step_nav_kb(back_cb="wiz_back_rate"),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_rate)
    await callback.answer()


# ==============================================================================
# STEP 2: EXCHANGE RATE
# ==============================================================================

@offers_router.callback_query(OfferCreateFSM.waiting_for_rate, F.data == "wiz_back_rate")
async def back_to_offer_type(callback: CallbackQuery, state: FSMContext):
    """Navigate back from Rate input to Summary (if jump-editing) or Offer Type."""
    data = await state.get_data()
    if data.get("return_to_summary"):
        await _show_wizard_summary(callback.message, state)
        await callback.answer()
        return

    text, kb = build_offer_type_screen(back_to_main=True)
    await callback.message.edit_text(
        text,
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
        if not rate or rate <= 0 or rate > Decimal("999999999"):
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
            await _show_wizard_summary(message, state)
            return

        updated_data = await state.get_data()
        header = format_wizard_draft_header(updated_data, "min_limit")
        await message.answer(
            f"{header}"
            f"📦 **تحديد الحد الأدنى**\n"
            f"أدخل أقل كمية تقبل تنفيذها في الصفقة الواحدة (بـ `USDT`):",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_min"),
            parse_mode="Markdown"
        )
        await state.set_state(OfferCreateFSM.waiting_for_min_limit)
    except Exception:
        await message.answer(
            "⚠️ **سعر الصرف غير صالح**\n\n"
            "يرجى إدخال رقم صحيح وموجب لسعر الصرف:",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_rate"),
            parse_mode="Markdown"
        )


async def edit_rate_input(callback: CallbackQuery, state: FSMContext):
    """Prompt user to re-enter exchange rate."""
    data = await state.get_data()
    header = format_wizard_draft_header(data, "rate")
    await callback.message.edit_text(
        f"{header}"
        f"💵 **تعديل سعر الصرف**\n"
        f"أدخل سعر الصرف الجديد بالليرة السورية مقابل كل `1 USDT`:",
        reply_markup=get_step_nav_kb(back_cb="wiz_back_rate"),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_rate)
    await callback.answer()


# ==============================================================================
# STEP 3: MIN LIMIT
# ==============================================================================

@offers_router.callback_query(OfferCreateFSM.waiting_for_min_limit, F.data == "wiz_back_min")
async def back_to_rate(callback: CallbackQuery, state: FSMContext):
    """Navigate back from Min Limit to Summary (if jump-editing) or Rate input."""
    data = await state.get_data()
    if data.get("return_to_summary"):
        restore_updates = {}
        if data.get("prev_min_limit") is not None:
            restore_updates["min_limit"] = data["prev_min_limit"]
            restore_updates["candidate_min_limit"] = data["prev_min_limit"]
        if data.get("prev_max_limit") is not None:
            restore_updates["max_limit"] = data["prev_max_limit"]
            restore_updates["candidate_max_limit"] = data["prev_max_limit"]
        if restore_updates:
            await state.update_data(**restore_updates)
        await _show_wizard_summary(callback.message, state)
        await callback.answer()
        return

    header = format_wizard_draft_header(data, "rate")
    await callback.message.edit_text(
        f"{header}"
        f"💵 **تحديد سعر الصرف**\n"
        f"أدخل سعر الصرف بالليرة السورية مقابل كل `1 USDT`:",
        reply_markup=get_step_nav_kb(back_cb="wiz_back_rate"),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_rate)
    await callback.answer()


@offers_router.message(OfferCreateFSM.waiting_for_min_limit)
async def process_min_limit(message: Message, state: FSMContext):
    """Receive and validate min limit, then proceed to max limit."""
    try:
        min_limit = clean_numeric_input(message.text)
        if not min_limit or min_limit <= 0 or min_limit > Decimal("999999999"):
            raise ValueError()

        if min_limit < MIN_DEAL_AMOUNT_USDT:
            await message.answer(
                "⚠️ أقل كمية مسموحة هي 1 USDT.\n\n"
                "يرجى إدخال حد أدنى لا يقل عن `1 USDT`:",
                reply_markup=get_step_nav_kb(back_cb="wiz_back_min"),
                parse_mode="Markdown"
            )
            return

        min_val = str(min_limit)
        data = await state.get_data()
        history = push_wizard_step(data.get("step_history", []), "min_limit", {"min_limit": min_val})

        await state.update_data(
            min_limit=min_val,
            candidate_min_limit=min_val,
            step_history=history
        )

        min_str = format_usdt(min_limit)
        updated_data = await state.get_data()
        header = format_wizard_draft_header(updated_data, "max_limit")
        await message.answer(
            f"{header}"
            f"📦 **تحديد الحد الأقصى**\n"
            f"أدخل أعلى كمية إجمالية معروضة للتداول (يجب أن تكون أكبر من أو تساوي `{min_str} USDT`):",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_max"),
            parse_mode="Markdown"
        )
        await state.set_state(OfferCreateFSM.waiting_for_max_limit)
    except Exception:
        await message.answer(
            "⚠️ **قيمة غير صالحة**\n\n"
            "يرجى إدخال رقم صحيح وموجب للحد الأدنى (بـ `USDT`):",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_min"),
            parse_mode="Markdown"
        )


async def edit_min_limit_input(callback: CallbackQuery, state: FSMContext):
    """Prompt user to re-enter min limit."""
    data = await state.get_data()
    header = format_wizard_draft_header(data, "min_limit")
    await callback.message.edit_text(
        f"{header}"
        "📦 **تعديل الحد الأدنى**\n"
        "أدخل الحد الأدنى الجديد للكمية المسموح بها في الصفقة (بـ `USDT`):",
        reply_markup=get_step_nav_kb(back_cb="wiz_back_min"),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_min_limit)
    await callback.answer()


# ==============================================================================
# STEP 4: MAX LIMIT
# ==============================================================================

@offers_router.callback_query(OfferCreateFSM.waiting_for_max_limit, F.data == "wiz_back_max")
async def back_to_min_limit(callback: CallbackQuery, state: FSMContext):
    """Navigate back from Max Limit to Min Limit input."""
    data = await state.get_data()
    header = format_wizard_draft_header(data, "min_limit")
    await callback.message.edit_text(
        f"{header}"
        "📦 **تحديد الحد الأدنى**\n"
        "أدخل أقل كمية تقبل تنفيذها في الصفقة الواحدة (بـ `USDT`):",
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
        if not max_limit or max_limit <= 0 or max_limit > Decimal("999999999"):
            raise ValueError()

        data = await state.get_data()
        min_limit = Decimal(str(data.get("min_limit", "0")))

        if max_limit < min_limit:
            await message.answer(
                f"⚠️ **الحد الأقصى غير متطابق**\n\n"
                f"يجب أن يكون أكبر من أو يساوي الحد الأدنى (`{format_usdt(min_limit)} USDT`).\n"
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

        updated_data = await state.get_data()
        selected = updated_data.get("payment_methods", [])
        header = format_wizard_draft_header(updated_data, "methods")
        await message.answer(
            f"{header}"
            "💳 **وسائل الدفع المقبولة**\n"
            "اختر وسائل الدفع المعتمدة لديك (يمكنك تحديد أكثر من وسيلة)، ثم اضغط على **متابعة**:",
            reply_markup=get_payment_methods_kb(selected),
            parse_mode="Markdown"
        )
        await state.set_state(OfferCreateFSM.waiting_for_methods)
    except Exception:
        await message.answer(
            "⚠️ **قيمة غير صالحة**\n\n"
            "يرجى إدخال رقم صحيح وموجب للحد الأقصى (بـ `USDT`):",
            reply_markup=get_step_nav_kb(back_cb="wiz_back_max"),
            parse_mode="Markdown"
        )


async def edit_max_limit_input(callback: CallbackQuery, state: FSMContext):
    """Prompt user to re-enter max limit."""
    data = await state.get_data()
    header = format_wizard_draft_header(data, "max_limit")
    await callback.message.edit_text(
        f"{header}"
        f"📦 **تعديل الحد الأقصى**\n"
        f"أدخل الحد الأقصى الجديد للكمية (بـ `USDT`):",
        reply_markup=get_step_nav_kb(back_cb="wiz_back_max"),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_max_limit)
    await callback.answer()


# ==============================================================================
# STEP 5: PAYMENT METHODS MULTI-SELECT
# ==============================================================================

@offers_router.callback_query(OfferCreateFSM.waiting_for_methods, F.data == "wiz_back_methods")
async def back_to_max_limit(callback: CallbackQuery, state: FSMContext):
    """Navigate back from Payment Methods to Summary (if jump-editing) or Max Limit input."""
    data = await state.get_data()
    if data.get("return_to_summary"):
        restore_updates = {}
        if data.get("prev_payment_methods") is not None:
            restore_updates["payment_methods"] = list(data["prev_payment_methods"])
        if data.get("prev_payout_details") is not None:
            restore_updates["payout_details"] = dict(data["prev_payout_details"])
        if restore_updates:
            await state.update_data(**restore_updates)
        await _show_wizard_summary(callback.message, state)
        await callback.answer()
        return

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
async def confirm_payment_methods(callback: CallbackQuery, state: FSMContext):
    """Validate selected methods and proceed directly to Negotiation Options or return to Summary."""
    data = await state.get_data()
    selected_methods = data.get("payment_methods", [])

    if not selected_methods:
        await callback.answer("⚠️ يرجى اختيار وسيلة دفع واحدة على الأقل للمتابعة.", show_alert=True)
        return

    history = push_wizard_step(data.get("step_history", []), "methods", {"payment_methods": selected_methods})
    await state.update_data(step_history=history)

    if data.get("return_to_summary"):
        await _show_wizard_summary(callback.message, state)
        await callback.answer("تم تحديث وسائل الدفع.")
        return

    await _show_negotiation_decision(callback.message, state)
    await state.set_state(OfferCreateFSM.waiting_for_negotiable_decision)
    await callback.answer()


# ==============================================================================
# STEP 6: NEGOTIATION OPTIONS
# ==============================================================================

async def _show_negotiation_decision(message: Message, state: FSMContext | None = None):
    """Render negotiation choices with Back and Cancel buttons."""
    data = await state.get_data() if state else {}
    header = format_wizard_draft_header(data, "negotiation")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤝 إتاحة التفاوض", callback_data="neg_choice_yes"),
            InlineKeyboardButton(text="🔒 بدون تفاوض", callback_data="neg_choice_no")
        ],
        [
            InlineKeyboardButton(text="🔙 رجوع", callback_data="wiz_back_negotiation"),
            InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")
        ]
    ])
    text = (
        f"{header}"
        "⚙️ **خيارات التفاوض على العرض**\n\n"
        "هل ترغب بالسماح للمتداولين بتقديم مقترحات تفاوض على السعر أو الكمية؟"
    )
    if hasattr(message, "edit_text"):
        await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@offers_router.callback_query(F.data == "wiz_back_negotiation")
async def back_to_payment_methods(callback: CallbackQuery, state: FSMContext):
    """Navigate back from Negotiation to Summary (if jump-editing) or Payment Methods."""
    data = await state.get_data()
    if data.get("return_to_summary"):
        await _show_wizard_summary(callback.message, state)
        await callback.answer()
        return

    selected = data.get("payment_methods", [])
    header = format_wizard_draft_header(data, "methods")
    await callback.message.edit_text(
        f"{header}"
        "💳 **وسائل الدفع المقبولة**\n"
        "اختر وسائل الدفع المعتمدة لديك (يمكنك تحديد أكثر من وسيلة)، ثم اضغط على **متابعة**:",
        reply_markup=get_payment_methods_kb(selected),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_methods)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_negotiable_decision, F.data.startswith("neg_choice_"))
async def process_negotiable_decision(callback: CallbackQuery, state: FSMContext):
    """Handle choice between open negotiation and fixed rate/amount."""
    choice = callback.data.split("_")[-1]
    data = await state.get_data()
    if choice == "yes":
        header = format_wizard_draft_header(data, "negotiation")
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
            f"{header}"
            "⚙️ **تحديد نطاق التفاوض**\n\n"
            "حدد البنود التي تسمح للطرف الآخر بالتفاوض عليها:",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        await state.set_state(OfferCreateFSM.waiting_for_negotiable_type)
    else:
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
            await _show_wizard_summary(callback.message, state)
        else:
            await _show_note_step(callback.message, state)
            await state.set_state(OfferCreateFSM.waiting_for_note)
    await callback.answer()


@offers_router.callback_query(F.data == "wiz_back_negotiation_choice")
async def back_to_negotiation_decision(callback: CallbackQuery, state: FSMContext):
    """Navigate back to negotiation yes/no choice."""
    await _show_negotiation_decision(callback.message, state)
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
        await _show_wizard_summary(callback.message, state)
    else:
        await _show_note_step(callback.message, state)
        await state.set_state(OfferCreateFSM.waiting_for_note)
    await callback.answer()


# ==============================================================================
# STEP 7: OFFER NOTES / CONDITIONS (SHAM CASH SUGGESTIONS OR CUSTOM NOTE)
# ==============================================================================

def get_offer_note_kb(payment_methods: list[str] | None) -> InlineKeyboardMarkup:
    """Build note step keyboard: includes 4 Sham Cash suggestions when Sham Cash is selected."""
    rows = []
    methods = payment_methods or []
    if "شام كاش ليرة" in methods:
        for idx, suggestion in enumerate(SHAM_CASH_NOTE_SUGGESTIONS):
            rows.append([InlineKeyboardButton(text=suggestion, callback_data=f"note_sug_{idx}")])

    rows.append([InlineKeyboardButton(text="تخطي", callback_data="note_skip")])
    rows.append([
        InlineKeyboardButton(text="رجوع ◀️", callback_data="wiz_back_note"),
        InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def _show_note_step(message: Message, state: FSMContext | None = None):
    """Render offer notes step (with Sham Cash quick suggestions if applicable)."""
    data = await state.get_data() if state else {}
    header = format_wizard_draft_header(data, "note")
    methods = data.get("payment_methods", [])
    kb = get_offer_note_kb(methods)
    text = (
        f"{header}"
        "📝 **ملاحظات وشروط العرض (اختياري، حتى 40 حرفاً)**\n\n"
        "أدخل ملاحظاتك أو شروطك الخاصة بالعرض، أو اضغط **تخطي** للمتابعة بدون ملاحظات:"
    )
    if hasattr(message, "edit_text"):
        try:
            await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@offers_router.callback_query(F.data == "wiz_back_note")
async def back_from_note_step(callback: CallbackQuery, state: FSMContext):
    """Navigate back from Note step to Summary (if jump-editing) or Negotiation decision."""
    data = await state.get_data()
    if data.get("return_to_summary"):
        await _show_wizard_summary(callback.message, state)
        await callback.answer()
        return

    await _show_negotiation_decision(callback.message, state)
    await state.set_state(OfferCreateFSM.waiting_for_negotiable_decision)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_note, F.data.startswith("note_sug_"))
async def process_note_suggestion(callback: CallbackQuery, state: FSMContext):
    """Select one of the pre-approved Sham Cash note suggestions (publishes immediately without manual review)."""
    idx_str = callback.data.replace("note_sug_", "")
    if not idx_str.isdigit():
        await callback.answer("خيار غير صالح.", show_alert=True)
        return
    idx = int(idx_str)
    if idx < 0 or idx >= len(SHAM_CASH_NOTE_SUGGESTIONS):
        await callback.answer("خيار غير صالح.", show_alert=True)
        return

    chosen_note = SHAM_CASH_NOTE_SUGGESTIONS[idx]
    data = await state.get_data()
    history = push_wizard_step(
        data.get("step_history", []),
        "note",
        {"note": chosen_note, "is_custom_note": False}
    )
    await state.update_data(
        note=chosen_note,
        is_custom_note=False,
        step_history=history
    )

    if data.get("return_to_summary"):
        await _show_wizard_summary(callback.message, state)
    else:
        await _show_duration_choice(callback.message, state)
        await state.set_state(OfferCreateFSM.waiting_for_duration_choice)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_note, F.data == "note_skip")
async def process_note_skip(callback: CallbackQuery, state: FSMContext):
    """Skip note step (no note attached to offer)."""
    data = await state.get_data()
    history = push_wizard_step(
        data.get("step_history", []),
        "note",
        {"note": None, "is_custom_note": False}
    )
    await state.update_data(
        note=None,
        is_custom_note=False,
        step_history=history
    )

    if data.get("return_to_summary"):
        await _show_wizard_summary(callback.message, state)
    else:
        await _show_duration_choice(callback.message, state)
        await state.set_state(OfferCreateFSM.waiting_for_duration_choice)
    await callback.answer()


@offers_router.message(OfferCreateFSM.waiting_for_note)
async def process_custom_note_text(message: Message, state: FSMContext):
    """Handle custom note text typed by the user (max 40 characters; requires admin approval unless identical to a preset)."""
    raw_text = (message.text or "").strip()
    cleaned_note = " ".join(raw_text.split())
    data = await state.get_data()
    methods = data.get("payment_methods", [])

    if not cleaned_note:
        await message.answer(
            "⚠️ يرجى كتابة نص الملاحظة (حتى 40 حرفاً) أو الضغط على **تخطي**:",
            reply_markup=get_offer_note_kb(methods),
            parse_mode="Markdown"
        )
        return

    if len(cleaned_note) > MAX_OFFER_NOTE_LENGTH:
        await message.answer(
            f"⚠️ **الملاحظة طويلة جداً ({len(cleaned_note)} حرفاً)**\n\n"
            f"الحد الأقصى المسموح به هو `{MAX_OFFER_NOTE_LENGTH}` حرفاً.\n"
            f"يرجى اختصار الملاحظة وإعادة إرسالها أو الضغط على **تخطي**:",
            reply_markup=get_offer_note_kb(methods),
            parse_mode="Markdown"
        )
        return

    is_custom = cleaned_note not in SHAM_CASH_NOTE_SUGGESTIONS
    history = push_wizard_step(
        data.get("step_history", []),
        "note",
        {"note": cleaned_note, "is_custom_note": is_custom}
    )
    await state.update_data(
        note=cleaned_note,
        is_custom_note=is_custom,
        step_history=history
    )

    if data.get("return_to_summary"):
        await _show_wizard_summary(message, state)
    else:
        await _show_duration_choice(message, state)
        await state.set_state(OfferCreateFSM.waiting_for_duration_choice)


# ==============================================================================
# STEP 8: OFFER DURATION & CONFIRMATION (3-STAGE BUTTON PICKER)
# ==============================================================================

async def _show_duration_choice(message: Message, state: FSMContext | None = None):
    """Render Stage 1 duration options (Default 4h vs Custom Hours+Minutes)."""
    data = await state.get_data() if state else {}
    header = format_wizard_draft_header(data, "duration")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏱️ الافتراضية (4 ساعات)", callback_data="dur_default_4")],
        [InlineKeyboardButton(text="⚙️ تحديد مدة مخصصة", callback_data="dur_custom")],
        [
            InlineKeyboardButton(text="🔙 رجوع", callback_data="wiz_back_duration"),
            InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")
        ]
    ])
    text = (
        f"{header}"
        "⏱️ **مدة صلاحية العرض في السوق**\n\n"
        "كم تريد أن يبقى العرض نشطاً ومتاحاً في القناة قبل انتهاء صلاحيته آلياً؟\n"
        "• اختر **الافتراضية (4 ساعات)** للاعتماد المباشر.\n"
        "• أو اختر **تحديد مدة مخصصة** لاختيار الساعات (0–3) ثم الدقائق (5–45)."
    )
    if hasattr(message, "edit_text"):
        try:
            await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@offers_router.callback_query(F.data == "wiz_back_duration")
async def back_to_note_step(callback: CallbackQuery, state: FSMContext):
    """Navigate back from Duration to Summary (if jump-editing) or Offer Notes step."""
    data = await state.get_data()
    if data.get("return_to_summary"):
        await _show_wizard_summary(callback.message, state)
        await callback.answer()
        return

    await _show_note_step(callback.message, state)
    await state.set_state(OfferCreateFSM.waiting_for_note)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_duration_choice, F.data == "dur_default_4")
async def process_duration_default(callback: CallbackQuery, state: FSMContext):
    """Set default 4 hours duration and show summary."""
    data = await state.get_data()
    history = push_wizard_step(data.get("step_history", []), "duration", {"duration_hours": 4.0})
    await state.update_data(duration_hours=4.0, step_history=history)
    await _show_wizard_summary(callback.message, state)
    await callback.answer()


@offers_router.callback_query(
    OfferCreateFSM(),
    F.data == "dur_custom"
)
async def prompt_custom_duration(callback: CallbackQuery, state: FSMContext):
    """Stage 2: Prompt user with 2x2 button grid to pick hours (0 to 3)."""
    data = await state.get_data()
    header = format_wizard_draft_header(data, "duration")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="0 ساعة", callback_data="dur_hr_0"),
            InlineKeyboardButton(text="1 ساعة", callback_data="dur_hr_1")
        ],
        [
            InlineKeyboardButton(text="2 ساعة", callback_data="dur_hr_2"),
            InlineKeyboardButton(text="3 ساعات", callback_data="dur_hr_3")
        ],
        [
            InlineKeyboardButton(text="🔙 رجوع", callback_data="wiz_back_custom_dur"),
            InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")
        ]
    ])
    await callback.message.edit_text(
        f"{header}"
        "⏱️ **تحديد مدة العرض — الساعات**\n\n"
        "اختر عدد الساعات (من 0 إلى 3):",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_custom_duration)
    await callback.answer()


@offers_router.callback_query(
    OfferCreateFSM.waiting_for_custom_duration,
    F.data.startswith("dur_hr_")
)
async def select_custom_duration_hour(callback: CallbackQuery, state: FSMContext):
    """Stage 3: Receive selected hour (0..3) and render 2-column minutes grid."""
    hours = int(callback.data.replace("dur_hr_", ""))
    await state.update_data(custom_dur_hours=hours)
    data = await state.get_data()
    header = format_wizard_draft_header(data, "duration")
    hr_label = f"{hours} ساعات" if hours >= 3 else f"{hours} ساعة"

    rows = [
        [
            InlineKeyboardButton(text="5 دقائق", callback_data="dur_min_5"),
            InlineKeyboardButton(text="10 دقائق", callback_data="dur_min_10")
        ],
        [
            InlineKeyboardButton(text="15 دقيقة", callback_data="dur_min_15"),
            InlineKeyboardButton(text="20 دقيقة", callback_data="dur_min_20")
        ],
        [
            InlineKeyboardButton(text="30 دقيقة", callback_data="dur_min_30"),
            InlineKeyboardButton(text="45 دقيقة", callback_data="dur_min_45")
        ]
    ]
    if hours > 0:
        rows.append([
            InlineKeyboardButton(text=f"⏱️ بدون دقائق إضافية ({hr_label} فقط)", callback_data="dur_min_0")
        ])
    rows.append([
        InlineKeyboardButton(text="🔙 رجوع", callback_data="dur_custom"),
        InlineKeyboardButton(text="❌ إلغاء", callback_data="wiz_cancel")
    ])

    await callback.message.edit_text(
        f"{header}"
        f"⏱️ **تحديد مدة العرض — الدقائق**\n\n"
        f"اختر الدقائق الإضافية (بعد {hr_label}):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="Markdown"
    )
    await callback.answer()


@offers_router.callback_query(
    OfferCreateFSM.waiting_for_custom_duration,
    F.data.startswith("dur_min_")
)
async def select_custom_duration_minutes(callback: CallbackQuery, state: FSMContext):
    """Commit custom hours + minutes duration and advance directly to final summary."""
    mins = int(callback.data.replace("dur_min_", ""))
    data = await state.get_data()
    hours = int(data.get("custom_dur_hours", 0))

    total_hours = round(hours + (mins / 60.0), 4)
    if total_hours <= 0:
        await callback.answer("⚠️ يرجى اختيار مدة لا تقل عن 5 دقائق.", show_alert=True)
        return

    history = push_wizard_step(data.get("step_history", []), "duration", {"duration_hours": total_hours})
    await state.update_data(
        duration_hours=total_hours,
        candidate_duration=total_hours,
        step_history=history
    )
    await _show_wizard_summary(callback.message, state)
    await callback.answer()


@offers_router.callback_query(F.data == "wiz_back_custom_dur")
async def back_to_duration_choice(callback: CallbackQuery, state: FSMContext):
    """Navigate back from custom duration input to duration choice keyboard."""
    await _show_duration_choice(callback.message, state)
    await state.set_state(OfferCreateFSM.waiting_for_duration_choice)
    await callback.answer()


@offers_router.message(OfferCreateFSM.waiting_for_custom_duration)
async def process_custom_duration(message: Message, state: FSMContext):
    """Fallback text handler for custom duration (kept for backwards compatibility)."""
    try:
        hours_val = float(message.text.strip())
        if hours_val < (5.0 / 60.0) or hours_val > 4.0:
            raise ValueError()

        await state.update_data(candidate_duration=hours_val)
        dur_str = format_duration_hours_minutes(hours_val)
        kb = get_confirm_step_kb(
            confirm_cb="wiz_confirm_dur",
            edit_cb="wiz_edit_dur",
            back_cb="wiz_back_custom_dur"
        )
        await message.answer(
            "⏱️ **تأكيد مدة الصلاحية**\n\n"
            f"▫️ **مدة بقاء العرض في السوق:** `{dur_str}`\n\n"
            "هل ترغب باعتماد هذه المدة والمتابعة إلى المراجعة النهائية؟",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        await state.set_state(OfferCreateFSM.confirm_custom_duration)
    except Exception:
        await message.answer(
            "⚠️ **قيمة غير صالحة**\n\n"
            "يرجى اختيار الساعات والدقائق من الأزرار أعلاه، أو إدخال رقم بين 1 و 4:",
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
    """Render full wizard summary with jump-edit options and clean temporary edit flags."""
    await state.update_data(
        return_to_summary=False,
        editing_methods_from_summary=False,
        prev_min_limit=None,
        prev_max_limit=None,
        prev_payment_methods=None,
        prev_payout_details=None,
    )
    data = await state.get_data()
    summary_text = format_wizard_summary(data, bot_name=BOT_NAME)
    kb = get_wizard_summary_kb()

    if hasattr(message, "edit_text"):
        try:
            await message.edit_text(summary_text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await message.answer(summary_text, reply_markup=kb, parse_mode="Markdown")
    else:
        await message.answer(summary_text, reply_markup=kb, parse_mode="Markdown")
    await state.set_state(OfferCreateFSM.waiting_for_confirm_publish)


@offers_router.callback_query(F.data == "wiz_back_summary")
async def back_from_summary(callback: CallbackQuery, state: FSMContext):
    """Navigate back from Summary to Duration choice."""
    await _show_duration_choice(callback.message, state)
    await state.set_state(OfferCreateFSM.waiting_for_duration_choice)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_confirm_publish, F.data == "wiz_jump_rate")
async def jump_edit_rate(callback: CallbackQuery, state: FSMContext):
    """Jump from summary to edit rate."""
    await state.update_data(return_to_summary=True)
    await edit_rate_input(callback, state)


@offers_router.callback_query(OfferCreateFSM.waiting_for_confirm_publish, F.data == "wiz_jump_limits")
async def jump_edit_limits(callback: CallbackQuery, state: FSMContext):
    """Jump from summary to edit min/max limits with backup of current limits."""
    data = await state.get_data()
    await state.update_data(
        return_to_summary=True,
        prev_min_limit=data.get("min_limit"),
        prev_max_limit=data.get("max_limit"),
    )
    await edit_min_limit_input(callback, state)


@offers_router.callback_query(OfferCreateFSM.waiting_for_confirm_publish, F.data == "wiz_jump_methods")
async def jump_edit_methods(callback: CallbackQuery, state: FSMContext):
    """Jump from summary to edit payment methods with backup of current methods."""
    data = await state.get_data()
    selected = list(data.get("payment_methods", []))
    await state.update_data(
        return_to_summary=True,
        prev_payment_methods=list(selected),
    )
    header = format_wizard_draft_header(data, "methods")
    await callback.message.edit_text(
        f"{header}"
        "💳 **وسائل الدفع المقبولة**\n"
        "اختر وسائل الدفع المعتمدة لديك (يمكنك تحديد أكثر من وسيلة)، ثم اضغط على **متابعة**:",
        reply_markup=get_payment_methods_kb(selected),
        parse_mode="Markdown"
    )
    await state.set_state(OfferCreateFSM.waiting_for_methods)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_confirm_publish, F.data == "wiz_jump_neg")
async def jump_edit_negotiation(callback: CallbackQuery, state: FSMContext):
    """Jump from summary to edit negotiation options."""
    await state.update_data(return_to_summary=True)
    await _show_negotiation_decision(callback.message, state)
    await state.set_state(OfferCreateFSM.waiting_for_negotiable_decision)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_confirm_publish, F.data == "wiz_jump_note")
async def jump_edit_note(callback: CallbackQuery, state: FSMContext):
    """Jump from summary to edit offer note."""
    await state.update_data(return_to_summary=True)
    await _show_note_step(callback.message, state)
    await state.set_state(OfferCreateFSM.waiting_for_note)
    await callback.answer()


@offers_router.callback_query(OfferCreateFSM.waiting_for_confirm_publish, F.data == "wiz_jump_dur")
async def jump_edit_duration(callback: CallbackQuery, state: FSMContext):
    """Jump from summary to edit offer duration."""
    await state.update_data(return_to_summary=True)
    await _show_duration_choice(callback.message, state)
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

    # 1b. Active deals cap check (max 5 active deals)
    active_deals_count = await count_user_active_deals(db_session, user_id)
    if active_deals_count >= MAX_ACTIVE_DEALS:
        await state.update_data(is_publishing=False)
        await callback.answer(
            "لديك 5 صفقات غير مكتملة يرجى متابعتهم (إلغاء أو استكمال) لتتمكن من إجراء معاملات جديدة.",
            show_alert=True,
        )
        await callback.message.answer(
            build_max_active_deals_notice_text(),
            reply_markup=build_max_active_deals_kb(),
            parse_mode="Markdown",
        )
        return

    # 2. Section 6.1.1: Active offers cap check (max 5 active/pending offers for non-admins)
    if user_id not in settings.ADMIN_IDS:
        count_res = await db_session.execute(
            select(func.count(Offer.id)).where(
                Offer.user_id == user_id,
                Offer.status.in_(['active', 'reserved', 'negotiating', 'pending_approval'])
            )
        )
        active_offers_count = count_res.scalar() or 0
        if active_offers_count >= 5:
            await state.update_data(is_publishing=False)
            await callback.answer(
                "⚠️ تجاوزت الحد الأقصى المسموح به (5 عروض نشطة أو قيد المراجعة). يرجى إلغاء أو إكمال أحد عروضك الحالية أولاً.",
                show_alert=True
            )
            return

        # 2b. Rule 10: 15-minute cooldown between publishing two similar offers (same offer_type)
        similar_cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
        recent_similar_res = await db_session.execute(
            select(Offer.created_at)
            .where(
                Offer.user_id == user_id,
                Offer.offer_type == data["offer_type"],
                Offer.status.not_in(["cancelled", "deleted", "expired"]),
                Offer.created_at >= similar_cutoff,
            )
            .order_by(Offer.created_at.desc())
            .limit(1)
        )
        recent_created_at = recent_similar_res.scalar_one_or_none()
        if recent_created_at is not None:
            if recent_created_at.tzinfo is None:
                recent_created_at = recent_created_at.replace(tzinfo=timezone.utc)
            rem_seconds = max(
                1,
                int(((recent_created_at + timedelta(minutes=15)) - datetime.now(timezone.utc)).total_seconds()),
            )
            rem_minutes = max(1, (rem_seconds + 59) // 60)
            await state.update_data(is_publishing=False)
            await callback.answer(
                f"⏳ يمكنك نشر عرض مماثل جديد بعد مرور {rem_minutes} دقيقة من عرضك السابق.",
                show_alert=True,
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
    primary_payout_str = extract_primary_payout_info(data)
    payout_details_json = data.get("payout_details", {}) or {}
    note_val = data.get("note")
    is_custom_note = bool(data.get("is_custom_note", False) and note_val)

    initial_status = "pending_approval" if is_custom_note else "active"
    expires_at = None if is_custom_note else (now + timedelta(hours=duration_hours))

    new_offer = Offer(
        user_id=user_id,
        offer_type=data["offer_type"],
        rate=Decimal(str(data["rate"])),
        min_limit=Decimal(str(data["min_limit"])),
        max_limit=Decimal(str(data["max_limit"])),
        payment_methods=data["payment_methods"],
        payout_info=primary_payout_str,  # Backward compatibility with deals.py
        payout_details=payout_details_json,  # New JSONB storage
        note=note_val,
        duration_hours=Decimal(str(duration_hours)),
        negotiable=data.get("negotiable", False),
        negotiable_type=data.get("negotiable_type", "none"),
        expires_at=expires_at,
        status=initial_status,
        status_changed_at=now
    )
    db_session.add(new_offer)
    await db_session.commit()
    await db_session.refresh(new_offer)

    # 5. Presentation formatters
    duration_str = format_duration_hours_minutes(duration_hours)
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
    note_line_md = f"📝 **الملاحظة :** {escape_markdown(str(new_offer.note))}\n" if new_offer.note else ""

    if is_custom_note:
        await callback.message.edit_text(
            f"⏳ **تم إرسال العرض للمراجعة — فرصة رقم : {new_offer.id}**\n"
            "〰️〰️〰️〰️〰️〰️〰️\n"
            f"⚖️ **النوع :** {offer_type_ar}\n"
            f"💰 **الكمية :** من `{min_str}` إلى `{max_str} USDT`\n"
            f"📊 **سعر الصرف :** `{rate_str}` ل.س\n"
            f"🏦 **طرق الدفع :** {methods_str}\n"
            f"🤝 **التفاوض :** {neg_status_ar}\n"
            f"{note_line_md}"
            f"⏳ **مدة المعاملة :** {duration_str} *(تبدأ فور الاعتماد)*\n"
            "〰️〰️〰️〰️〰️〰️〰️\n"
            "ℹ️ نظراً لإضافة **ملاحظة مخصصة**، تم إرسال العرض إلى الإدارة للمراجعة السريعة، وسيتم نشره تلقائياً في السوق والقناة فور الموافقة عليه.",
            parse_mode="Markdown"
        )
        await state.clear()
        await callback.answer("تم إرسال العرض لموافقة الإدارة.")

        trader_res = await db_session.execute(select(User).where(User.telegram_id == user_id))
        trader_user = trader_res.scalar_one_or_none()
        trader_tag = safe_username(trader_user.username if trader_user else callback.from_user.username)
        trader_nick = escape_markdown(trader_user.nickname) if (trader_user and trader_user.nickname) else "—"

        admin_card_text = (
            f"📝 **طلب اعتماد عرض بملاحظة مخصصة — عرض #{new_offer.id}**\n"
            "〰️〰️〰️〰️〰️〰️〰️\n"
            f"👤 **صاحب العرض:** `{user_id}` ({trader_tag} | اللقب: {trader_nick})\n"
            f"⚖️ **النوع:** {offer_type_ar}\n"
            f"💰 **الكمية:** من `{min_str}` إلى `{max_str} USDT`\n"
            f"📊 **سعر الصرف:** `{rate_str}` ل.س\n"
            f"🏦 **طرق الدفع:** {methods_str}\n"
            f"🤝 **التفاوض:** {neg_status_ar}\n"
            f"⏳ **المدة المطلوبة:** {duration_str}\n"
            "〰️〰️〰️〰️〰️〰️〰️\n"
            f"📝 **الملاحظة المخصصة للمراجعة:**\n"
            f"`{escape_markdown(str(new_offer.note))}`"
        )
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ موافقة ونشر العرض", callback_data=f"adm_note_app_{new_offer.id}"),
                InlineKeyboardButton(text="❌ رفض مع ذكر السبب", callback_data=f"adm_note_rej_{new_offer.id}")
            ]
        ])
        await dispatch_admin_notification(
            bot=bot,
            category="notes",
            text=admin_card_text,
            reply_markup=admin_kb,
            parse_mode="Markdown"
        )
        return

    await callback.message.edit_text(
        f"✅ **تم نشر العرض بنجاح — فرصة رقم : {new_offer.id}**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"⚖️ **النوع :** {offer_type_ar}\n"
        f"💰 **الكمية :** من `{min_str}` إلى `{max_str} USDT`\n"
        f"📊 **سعر الصرف :** `{rate_str}` ل.س\n"
        f"🏦 **طرق الدفع :** {methods_str}\n"
        f"🤝 **التفاوض :** {neg_status_ar}\n"
        f"{note_line_md}"
        f"⏳ **مدة المعاملة :** {duration_str}",
        parse_mode="Markdown"
    )
    await state.clear()
    await callback.answer("تم النشر بنجاح.")

    # 6. Broadcast to Channel using unified renderer (no payout details in public channel!)
    if settings.CHANNEL_ID and settings.BOT_USERNAME:
        trader_res = await db_session.execute(select(User).where(User.telegram_id == user_id))
        trader_user = trader_res.scalar_one_or_none()
        channel_text, kb_chan = build_channel_offer_message(
            new_offer,
            duration_override=duration_str,
            trader=trader_user,
        )
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
            "📊 **سوق العروض**\n"
            "〰️〰️〰️〰️〰️〰️〰️\n"
            "لا توجد عروض نشطة في السوق حالياً.\n"
            "يمكنك إنشاء عرض جديد عبر `/create_offer` أو من القائمة الرئيسية.",
            parse_mode="Markdown"
        )
        return
        
    maker_ids = list({off.user_id for off in offers if off.user_id})
    traders_map = {}
    if maker_ids:
        t_res = await db_session.execute(select(User).where(User.telegram_id.in_(maker_ids)))
        traders_map = {u.telegram_id: u for u in t_res.scalars().all()}

    for offer in offers:
        buttons = build_offer_action_buttons(
            offer_id=offer.id,
            offer_user_id=offer.user_id,
            viewer_id=message.from_user.id,
            negotiable=offer.negotiable,
            negotiable_type=offer.negotiable_type,
            context="offers",
            offer_type=offer.offer_type,
        )
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        rate_str = format_syp(offer.rate)
        min_str = format_usdt(offer.min_limit)
        max_str = format_usdt(offer.max_limit)
        offer_type_ar = '🔴 بيع "USDT"' if offer.offer_type == 'sell' else '🟢 شراء "USDT"'
        methods_str = format_payment_methods(offer.payment_methods)
        remaining_str = humanize_time_remaining(offer.expires_at, "4 ساعات")
        trader_trust_str = format_trader_trust_markdown(traders_map.get(offer.user_id))
        
        neg_type_labels = {
            "none": "غير متاح 🔒",
            "amount": "متاح على الكمية 📦",
            "rate": "متاح على السعر 💵",
            "both": "متاح على السعر والكمية ⚖️"
        }
        neg_str = neg_type_labels.get(offer.negotiable_type, "غير متاح 🔒") if offer.negotiable else "غير متاح 🔒"
        note_line = f"📝 **الملاحظة :** {escape_markdown(str(offer.note))}\n" if offer.note else ""

        text = (
            f"📦 **فرصة رقم : {offer.id}** (عرض #{offer.id})\n"
            f"⚖️ **النوع :** {offer_type_ar}\n"
            f"👤 **التاجر :** {trader_trust_str}\n"
            f"〰️〰️〰️〰️〰️〰️〰️\n"
            f"💰 **الكمية :** من `{min_str}` إلى `{max_str} USDT`\n"
            f"📊 **سعر الصرف :** `{rate_str}` ل.س\n"
            f"🏦 **طرق الدفع :** {methods_str}\n"
            f"🤝 **التفاوض :** {neg_str}\n"
            f"{note_line}"
            f"⏳ **مدة المعاملة :** {remaining_str}"
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

    try:
        await update_channel_offer_message(callback.bot, offer)
    except Exception as e:
        logger.warning(f"Failed to update channel message for deleted offer #{offer.id}: {e}")

    await callback.answer("تم إلغاء العرض بنجاح.")
    await callback.message.edit_text(
        f"❌ **تم إلغاء العرض #{offer.id}**\n\n"
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
        f"⏱️ **تمديد صلاحية العرض #{offer.id}**\n\n"
        f"اختر المدة الإضافية لإبقاء العرض نشطاً في السوق:",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await callback.answer()

@offers_router.callback_query(F.data.startswith("ext_dur_"))
async def handle_extend_offer_duration(callback: CallbackQuery, db_session: AsyncSession):
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

    try:
        await update_channel_offer_message(callback.bot, offer)
    except Exception as e:
        logger.warning(f"Failed to update channel message for extended offer #{offer.id}: {e}")

    expires_str = format_local_datetime(offer.expires_at)
    await callback.answer("تم تمديد صلاحية العرض بنجاح.")
    await callback.message.edit_text(
        f"✅ **تم تمديد صلاحية العرض #{offer.id}**\n\n"
        f"▫️ **المدة الإضافية:** `{hours}` ساعة\n"
        f"▫️ **الصلاحية حتى:**\n`{expires_str}`\n\n"
        f"العرض نشط ومتاح حالياً في السوق والقناة الرسمية.",
        parse_mode="Markdown"
    )

@offers_router.callback_query(F.data.startswith("ext_cancel_"))
async def handle_extend_cancel(callback: CallbackQuery):
    offer_id = int(callback.data.split("_")[-1])
    action_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏱️ تمديد الصلاحية", callback_data=f"ext_off_{offer_id}"),
            InlineKeyboardButton(text="❌ حذف العرض", callback_data=f"del_off_{offer_id}")
        ],
        [
            InlineKeyboardButton(text="📋 إدارة عروضي", callback_data="menu_my_offers")
        ]
    ])
    await callback.message.edit_text(
        f"📋 **إدارة العرض #{offer_id}**\n\n"
        f"تم التراجع عن تعديل الصلاحية. اختر الإجراء المطلوب للعرض من الأزرار أدناه:",
        reply_markup=action_kb,
        parse_mode="Markdown"
    )
    await callback.answer()
