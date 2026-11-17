import math
from aiogram import Router, F, Bot
from aiogram.filters import CommandStart, Command
from aiogram.filters.command import CommandObject
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func
from db.models import User, Offer, Deal
from aiogram.fsm.context import FSMContext
from bot.states.flow_states import OfferCreateFSM, FeedbackFSM
from config.settings import settings

# Import formatting utilities
from bot.handlers.offers import (
    format_syp,
    format_usdt,
    format_payment_methods,
    humanize_time_remaining,
    start_offer_creation
)
from bot.utils.deal_views import build_offer_action_buttons
from bot.handlers.deals import my_deals
from bot.utils.support_texts import (
    SUPPORT_FAQ_TEXT,
    TERMS_AND_CONDITIONS_TEXT,
    get_support_contact_text,
    split_terms_sections,
    split_faq_items
)
from bot.utils.branding import BOT_NAME
from bot.utils.fees import format_fee_rule_line

base_router = Router()

def get_main_menu_kb(user_id: int | None = None) -> InlineKeyboardMarkup:
    """Returns the primary menu buttons in clean visual hierarchy, including admin panel for admins."""
    keyboard = [
        [
            InlineKeyboardButton(text="➕ إنشاء عرض", callback_data="menu_create_offer"),
            InlineKeyboardButton(text="📊 السوق", callback_data="menu_browse_offers")
        ],
        [
            InlineKeyboardButton(text="👤 حسابي", callback_data="menu_user_profile"),
            InlineKeyboardButton(text="💼 صفقاتي", callback_data="main_my_deals")
        ],
        [
            InlineKeyboardButton(text="📋 عروضي", callback_data="menu_my_offers"),
            InlineKeyboardButton(text="💬 الدعم", callback_data="menu_support")
        ],
        [
            InlineKeyboardButton(text="📜 الشروط والأحكام", callback_data="menu_terms")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def get_back_to_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 الرئيسية", callback_data="menu_back_main")]
    ])

# ================= 1. /start ENTRYPOINT =================

@base_router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, db_session: AsyncSession, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username
    
    # Simple upsert logic for user
    from sqlalchemy.dialects.postgresql import insert
    stmt = insert(User).values(telegram_id=user_id, username=username)
    stmt = stmt.on_conflict_do_update(
        index_elements=['telegram_id'],
        set_=dict(username=username)
    )
    await db_session.execute(stmt)
    await db_session.commit()
    
    # Deep link to specific offer
    if command.args and command.args.startswith("offer_"):
        offer_id_str = command.args.split("_")[1]
        if offer_id_str.isdigit():
            offer_id = int(offer_id_str)
            result = await db_session.execute(select(Offer).where(Offer.id == offer_id))
            offer = result.scalar_one_or_none()
            if offer and offer.status == "active":
                offer_type_label = '🔴 بيع "USDT"' if offer.offer_type == 'sell' else '🟢 شراء "USDT"'
                rate_str = format_syp(offer.rate)
                min_str = format_usdt(offer.min_limit)
                max_str = format_usdt(offer.max_limit)
                methods_str = format_payment_methods(offer.payment_methods)

                neg_type_labels = {
                    "none": "غير متاح",
                    "amount": "متاح على الكمية",
                    "rate": "متاح على السعر",
                    "both": "متاح على السعر والكمية"
                }
                neg_str = neg_type_labels.get(offer.negotiable_type, "غير متاح") if offer.negotiable else "غير متاح"

                buttons = build_offer_action_buttons(
                    offer_id=offer.id,
                    offer_user_id=offer.user_id,
                    viewer_id=user_id,
                    negotiable=offer.negotiable,
                    negotiable_type=offer.negotiable_type,
                    context="start"
                )
                kb = InlineKeyboardMarkup(inline_keyboard=buttons)

                await message.answer(
                    f"📦 **فرصة رقم : {offer.id}**\n"
                    f"⚖️ **النوع :** {offer_type_label}\n"
                    f"〰️〰️〰️〰️〰️〰️〰️\n"
                    f"💰 **الكمية :** من `{min_str}` إلى `{max_str} USDT`\n"
                    f"📊 **سعر الصرف :** `{rate_str}` ل.س\n"
                    f"🏦 **طرق الدفع :** {methods_str}\n"
                    f"🤝 **التفاوض :** {neg_str}\n"
                    f"〰️〰️〰️〰️〰️〰️〰️\n"
                    f"{format_fee_rule_line()}",
                    reply_markup=kb,
                    parse_mode="Markdown"
                )
                return
            else:
                await message.answer(
                    "⚠️ **هذا العرض لم يعد متاحاً حالياً** (محجوز أو مكتمل).",
                    reply_markup=get_back_to_menu_kb(),
                    parse_mode="Markdown"
                )
                return

    welcome_text = (
        f"\u200f🛡️ **{BOT_NAME}**\n"
        f"ID : `{user_id}`\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        "وسيط آلي لتداول `USDT (BEP-20)` مقابل الليرة السورية بنظام الضمان."
    )
    await message.answer(welcome_text, reply_markup=get_main_menu_kb(user_id), parse_mode="Markdown")


@base_router.callback_query(F.data == "menu_back_main")
async def cq_back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    user_id = callback.from_user.id
    welcome_text = (
        f"\u200f🛡️ **{BOT_NAME}**\n"
        f"ID : `{user_id}`\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        "اختر من القائمة أدناه:"
    )
    try:
        await callback.message.edit_text(welcome_text, reply_markup=get_main_menu_kb(user_id), parse_mode="Markdown")
    except Exception:
        await callback.message.answer(welcome_text, reply_markup=get_main_menu_kb(user_id), parse_mode="Markdown")
    await callback.answer()


# ================= 2. BUTTON 1: CREATE OFFER (BUY / SELL CHOICE) =================

@base_router.callback_query(F.data == "menu_create_offer")
async def cq_menu_create_offer(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    # Check if user is banned
    user_res = await db_session.execute(
        select(User).where(User.telegram_id == callback.from_user.id)
    )
    user = user_res.scalar_one_or_none()
    if user and user.is_banned:
        await callback.answer(f"🚫 حسابك محظور من إنشاء عروض جديدة على {BOT_NAME}.", show_alert=True)
        return

    await state.clear()
    await state.update_data(
        step_history=[],
        payout_details={},
        payment_methods=[]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟢 شراء USDT", callback_data="offer_type_buy"),
            InlineKeyboardButton(text="🔴 بيع USDT", callback_data="offer_type_sell")
        ],
        [
            InlineKeyboardButton(text="🔙 الرئيسية", callback_data="menu_back_main")
        ]
    ])
    text = (
        "➕ **إنشاء عرض جديد**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        "🟢 **شراء USDT :** تدفع ليرة سورية وتستلم `USDT`\n"
        "🔴 **بيع USDT :** تودع `USDT` وتستلم ليرة سورية"
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await state.set_state(OfferCreateFSM.waiting_for_type)
    await callback.answer()


# ================= 3. BUTTON 2: BROWSE OFFERS WITH PAGINATION =================

@base_router.callback_query(F.data == "menu_browse_offers")
async def cq_menu_browse_offers(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🟢 شراء USDT", callback_data="browse_type_sell_0"),
            InlineKeyboardButton(text="🔴 بيع USDT", callback_data="browse_type_buy_0")
        ],
        [
            InlineKeyboardButton(text="🌐 كل العروض", callback_data="browse_type_all_0")
        ],
        [
            InlineKeyboardButton(text="🔙 الرئيسية", callback_data="menu_back_main")
        ]
    ])
    text = (
        "📊 **سوق العروض**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        "اختر نوع العملية المطلوبة:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


PAGE_SIZE = 4

@base_router.callback_query(F.data.startswith("browse_type_"))
async def cq_browse_paged_offers(callback: CallbackQuery, db_session: AsyncSession):
    parts = callback.data.split("_")
    target = parts[2]
    page = int(parts[3])

    query = select(Offer).where(Offer.status == "active")
    count_query = select(func.count(Offer.id)).where(Offer.status == "active")

    if target in ["sell", "buy"]:
        query = query.where(Offer.offer_type == target)
        count_query = count_query.where(Offer.offer_type == target)

    total_res = await db_session.execute(count_query)
    total_count = total_res.scalar() or 0

    total_pages = max(1, math.ceil(total_count / PAGE_SIZE))
    if page >= total_pages:
        page = total_pages - 1
    if page < 0:
        page = 0

    offset_val = page * PAGE_SIZE
    query = query.order_by(Offer.created_at.desc()).offset(offset_val).limit(PAGE_SIZE)
    result = await db_session.execute(query)
    offers = result.scalars().all()

    target_titles = {
        "sell": "🟢 عروض البيع (الشراء من المعلنين)",
        "buy": "🔴 عروض الشراء (البيع للمعلنين)",
        "all": "🌐 كافة العروض النشطة"
    }
    header = target_titles.get(target, "سوق العروض")

    if not offers:
        empty_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 تحديث السوق", callback_data=f"browse_type_{target}_0")],
            [InlineKeyboardButton(text="🔙 العودة للبحث", callback_data="menu_browse_offers")]
        ])
        await callback.message.edit_text(
            f"🔍 **{header}**\n\n"
            f"لا توجد عروض نشطة متوفرة في هذا القسم حالياً.\n"
            f"يمكنك إنشاء عرضك الخاص أو إعادة المحاولة لاحقاً.",
            reply_markup=empty_kb,
            parse_mode="Markdown"
        )
        await callback.answer()
        return

    text_lines = [
        f"🔍 **{header}**",
        f"📄 صفحة `{page + 1}` من `{total_pages}` | الإجمالي: `{total_count}`\n",
        f"{format_fee_rule_line()}\n"
    ]

    inline_buttons = []
    for off in offers:
        rate_str = format_syp(off.rate)
        min_str = format_usdt(off.min_limit)
        max_str = format_usdt(off.max_limit)
        off_type = "🔴 بيع" if off.offer_type == 'sell' else "🟢 شراء"
        methods = format_payment_methods(off.payment_methods)
        remaining = humanize_time_remaining(off.expires_at, "4 ساعات")
        neg = "متاح" if off.negotiable and off.negotiable_type != "none" else "ثابت"

        text_lines.append(
            f"🏷️ **عرض #{off.id}** — {off_type}\n"
            f"▫️ **سعر الصرف:** `{rate_str}` ل.س\n"
            f"▫️ **حدود الكمية:** من `{min_str} USDT` إلى `{max_str} USDT`\n"
            f"▫️ **وسائل الدفع:** {methods}\n"
            f"▫️ **التفاوض:** {neg}\n"
            f"▫️ **الصلاحية المتبقية:** {remaining}\n"
        )

        rows = build_offer_action_buttons(
            offer_id=off.id,
            offer_user_id=off.user_id,
            viewer_id=callback.from_user.id,
            negotiable=off.negotiable,
            negotiable_type=off.negotiable_type,
            context="browse"
        )
        if rows:
            inline_buttons.extend(rows)

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️ السابق", callback_data=f"browse_type_{target}_{page - 1}"))
    nav_row.append(InlineKeyboardButton(text=f"📄 {page + 1}/{total_pages}", callback_data="noop_page"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="التالي ▶️", callback_data=f"browse_type_{target}_{page + 1}"))
    inline_buttons.append(nav_row)

    inline_buttons.append([
        InlineKeyboardButton(text="🔄 تغيير الفلتر", callback_data="menu_browse_offers"),
        InlineKeyboardButton(text="🔙 الرئيسية", callback_data="menu_back_main")
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=inline_buttons)
    try:
        await callback.message.edit_text("\n".join(text_lines), reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer("\n".join(text_lines), reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@base_router.callback_query(F.data == "noop_page")
async def cq_noop_page(callback: CallbackQuery):
    await callback.answer("أنت تتصفح هذه الصفحة حالياً.")


# ================= 4. BUTTON 3: MANAGE MY OFFERS =================

@base_router.callback_query(F.data == "menu_my_offers")
async def cq_menu_my_offers(callback: CallbackQuery, db_session: AsyncSession):
    user_id = callback.from_user.id
    result = await db_session.execute(
        select(Offer).where(Offer.user_id == user_id).order_by(Offer.created_at.desc()).limit(8)
    )
    my_offers_list = result.scalars().all()

    if not my_offers_list:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ إنشاء عرض الآن", callback_data="menu_create_offer")],
            [InlineKeyboardButton(text="🔙 العودة للرئيسية", callback_data="menu_back_main")]
        ])
        await callback.message.edit_text(
            "📋 **إدارة عروضي**\n\n"
            "لم تقم بنشر أي عروض في السوق حتى الآن.\n"
            "يمكنك إنشاء عرض جديد من الزر أدناه.",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        await callback.answer()
        return

    status_badges = {
        "active": "🟢 نشط في السوق",
        "reserved": "🟡 محجوز بصفقة",
        "negotiating": "🔵 قيد التفاوض",
        "expired": "⚪ منتهي الصلاحية",
        "deleted": "🗑️ محذوف",
        "completed": "✅ مكتمل",
        "cancelled": "❌ ملغى"
    }

    text_lines = [
        "📋 **إدارة عروضي**\n",
        "أحدث العروض التي قمت بإنشائها:\n"
    ]

    buttons = []
    for off in my_offers_list:
        badge = status_badges.get(off.status, off.status)
        type_str = "🔴 بيع" if off.offer_type == 'sell' else "🟢 شراء"
        rate_str = format_syp(off.rate)
        min_str = format_usdt(off.min_limit)
        max_str = format_usdt(off.max_limit)

        text_lines.append(
            f"🏷️ **عرض #{off.id}** — {type_str}\n"
            f"▫️ **الحالة:** {badge}\n"
            f"▫️ **سعر الصرف:** `{rate_str}` ل.س\n"
            f"▫️ **حدود الكمية:** من `{min_str} USDT` إلى `{max_str} USDT`\n"
        )

        row = []
        if off.status == "active":
            row.append(InlineKeyboardButton(text=f"❌ حذف #{off.id}", callback_data=f"del_off_{off.id}"))
            row.append(InlineKeyboardButton(text=f"⏱️ تمديد #{off.id}", callback_data=f"ext_off_{off.id}"))
        if row:
            buttons.append(row)

    buttons.append([
        InlineKeyboardButton(text="➕ إنشاء عرض جديد", callback_data="menu_create_offer"),
        InlineKeyboardButton(text="🔙 الرئيسية", callback_data="menu_back_main")
    ])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    try:
        await callback.message.edit_text("\n".join(text_lines), reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer("\n".join(text_lines), reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


# ================= 5. BUTTON 4: SUPPORT & HELP SUB-MENU =================

@base_router.callback_query(F.data == "menu_support")
async def cq_menu_support(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💡 الأسئلة الشائعة", callback_data="supp_faq"),
            InlineKeyboardButton(text="📞 المساعدة والدعم", callback_data="supp_contact")
        ],
        [
            InlineKeyboardButton(text="✍️ تقديم اقتراح أو شكوى", callback_data="supp_feedback")
        ],
        [
            InlineKeyboardButton(text="🔙 العودة للرئيسية", callback_data="menu_back_main")
        ]
    ])
    text = (
        "💬 **مركز الدعم والمساعدة**\n\n"
        "اختر القسم المطلوب أدناه:\n\n"
        "▫️ **الأسئلة الشائعة:** دليل آلية الضمان والتداول والمدد الزمنية.\n"
        "▫️ **المساعدة والدعم:** قنوات التواصل المباشر مع إدارة المنصة.\n"
        "▫️ **تقديم اقتراح أو شكوى:** إرسال رسالة مباشرة لفريق الإدارة."
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@base_router.callback_query(F.data == "menu_terms")
async def cq_menu_terms(callback: CallbackQuery):
    preamble, sections = split_terms_sections()
    buttons = []
    for idx_str, title, _ in sections:
        buttons.append([InlineKeyboardButton(text=f"📄 {title}", callback_data=f"terms_sec_{idx_str}")])
    buttons.append([InlineKeyboardButton(text="📜 عرض النص الكامل", callback_data="terms_full")])
    buttons.append([InlineKeyboardButton(text="🔙 العودة للرئيسية", callback_data="menu_back_main")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    text = (
        f"📜 **شروط الاستخدام والأحكام العامة**\n\n"
        f"يرجى الاطلاع على بنود اتفاقية الاستخدام المنظمة لتداولات منصة {BOT_NAME}:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@base_router.callback_query(F.data == "terms_full")
async def cq_terms_full(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📑 فهرس البنود", callback_data="menu_terms")],
        [InlineKeyboardButton(text="🔙 العودة للرئيسية", callback_data="menu_back_main")]
    ])
    try:
        await callback.message.edit_text(TERMS_AND_CONDITIONS_TEXT, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(TERMS_AND_CONDITIONS_TEXT, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@base_router.callback_query(F.data.startswith("terms_sec_"))
async def cq_terms_section(callback: CallbackQuery):
    idx_str = callback.data.split("_")[-1]
    preamble, sections = split_terms_sections()
    total = len(sections)

    target_sec = None
    target_idx = 1
    for i, (s_idx, s_title, s_text) in enumerate(sections, 1):
        if s_idx == idx_str:
            target_sec = (s_idx, s_title, s_text)
            target_idx = i
            break

    if not target_sec:
        await callback.answer("البند غير موجود.", show_alert=True)
        return

    s_idx, s_title, s_text = target_sec

    nav_row = []
    if target_idx > 1:
        prev_idx = sections[target_idx - 2][0]
        nav_row.append(InlineKeyboardButton(text="◀️ السابق", callback_data=f"terms_sec_{prev_idx}"))
    nav_row.append(InlineKeyboardButton(text="📑 الفهرس", callback_data="menu_terms"))
    if target_idx < total:
        next_idx = sections[target_idx][0]
        nav_row.append(InlineKeyboardButton(text="التالي ▶️", callback_data=f"terms_sec_{next_idx}"))

    kb = InlineKeyboardMarkup(inline_keyboard=[
        nav_row,
        [InlineKeyboardButton(text="🔙 العودة للرئيسية", callback_data="menu_back_main")]
    ])

    text = s_text.strip()
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@base_router.callback_query(F.data == "supp_faq")
async def cq_supp_faq(callback: CallbackQuery):
    preamble, items = split_faq_items()
    buttons = []
    for idx_str, q_title, _ in items:
        buttons.append([InlineKeyboardButton(text=f"❓ {q_title}", callback_data=f"faq_item_{idx_str}")])
    buttons.append([InlineKeyboardButton(text="📜 عرض الدليل كاملاً", callback_data="faq_full")])
    buttons.append([InlineKeyboardButton(text="🔙 عودة لقسم الدعم", callback_data="menu_support")])
    buttons.append([InlineKeyboardButton(text="🏠 اللوحة الرئيسية", callback_data="menu_back_main")]
    )
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    text = (
        f"💡 **الأسئلة الشائعة**\n\n"
        f"اختر السؤال للاطلاع على إجابته وتفاصيل آلية التداول:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@base_router.callback_query(F.data == "faq_full")
async def cq_faq_full(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 قائمة الأسئلة", callback_data="supp_faq")],
        [InlineKeyboardButton(text="🏠 اللوحة الرئيسية", callback_data="menu_back_main")]
    ])
    try:
        await callback.message.edit_text(SUPPORT_FAQ_TEXT, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(SUPPORT_FAQ_TEXT, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@base_router.callback_query(F.data.startswith("faq_item_"))
async def cq_faq_item(callback: CallbackQuery):
    idx_str = callback.data.split("_")[-1]
    preamble, items = split_faq_items()

    target_item = None
    for s_idx, q_title, q_text in items:
        if s_idx == idx_str:
            target_item = (s_idx, q_title, q_text)
            break

    if not target_item:
        await callback.answer("السؤال غير موجود.", show_alert=True)
        return

    s_idx, q_title, q_text = target_item
    text = (
        f"💡 **الأسئلة الشائعة**\n\n"
        f"{q_text.strip()}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 قائمة الأسئلة", callback_data="supp_faq")],
        [InlineKeyboardButton(text="🏠 اللوحة الرئيسية", callback_data="menu_back_main")]
    ])
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@base_router.callback_query(F.data == "supp_contact")
async def cq_supp_contact(callback: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ تقديم شكوى / اقتراح الآن", callback_data="supp_feedback")],
        [InlineKeyboardButton(text="🔙 عودة لقسم الدعم", callback_data="menu_support")]
    ])
    await callback.message.edit_text(get_support_contact_text(), reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@base_router.callback_query(F.data == "supp_feedback")
async def cq_supp_feedback_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(FeedbackFSM.waiting_for_feedback_text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    await callback.message.edit_text(
        "✍️ **تقديم اقتراح أو شكوى**\n\n"
        "يرجى كتابة رسالتك بالتفصيل، مع ذكر رقم الصفقة في حال وجود ملاحظة مالية.\n"
        "سيتم تحويل رسالتك مباشرة إلى فريق الإدارة.\n\n"
        "أرسل النص الآن أو اضغط إلغاء:",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await callback.answer()


@base_router.message(FeedbackFSM.waiting_for_feedback_text)
async def process_user_feedback(message: Message, state: FSMContext, bot: Bot):
    feedback_text = (message.text or "").strip()
    if not feedback_text:
        await message.answer("يرجى كتابة نص الرسالة بشكل صحيح.")
        return

    await state.clear()
    user = message.from_user
    username_str = f"@{user.username}" if user.username else "بدون معرف"

    admin_alert = (
        "📩 **رسالة اقتراح أو شكوى جديدة**\n\n"
        f"▫️ **المستخدم:** {user.full_name}\n"
        f"▫️ **الرقم التعريفي:** `{user.id}`\n"
        f"▫️ **المعرف:** {username_str}\n\n"
        f"**نص الرسالة:**\n{feedback_text}"
    )

    for admin_id in settings.ADMIN_IDS:
        try:
            await bot.send_message(chat_id=admin_id, text=admin_alert, parse_mode="Markdown")
        except Exception:
            pass

    success_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏠 العودة للرئيسية", callback_data="menu_back_main")]
    ])
    await message.answer(
        "✅ **تم استلام رسالتك بنجاح**\n\n"
        "تم تحويل رسالتك إلى فريق الإدارة لمراجعتها ومتابعتها.",
        reply_markup=success_kb,
        parse_mode="Markdown"
    )


# ================= 6. EXISTING ACTIONS PRESERVATION =================

@base_router.callback_query(F.data == "main_browse_offers")
async def cq_browse_offers(callback: CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    await cq_menu_browse_offers(callback)

@base_router.callback_query(F.data == "main_create_offer")
async def cq_create_offer(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    await cq_menu_create_offer(callback, state, db_session)

@base_router.callback_query(F.data == "main_my_deals")
async def cq_my_deals(callback: CallbackQuery, db_session: AsyncSession):
    await callback.answer()
    class MockMessage:
        def __init__(self, original_msg, from_user):
            self.original_msg = original_msg
            self.from_user = from_user
        async def answer(self, *args, **kwargs):
            return await self.original_msg.answer(*args, **kwargs)
            
    mock_msg = MockMessage(callback.message, callback.from_user)
    await my_deals(mock_msg, db_session)

@base_router.callback_query(F.data.in_(["fsm_cancel_back", "main_cancel_op"]))
async def cq_cancel_fsm(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.answer("تم إلغاء العملية.")
    cancel_text = (
        "❌ **تم إلغاء العملية**\n\n"
        "يمكنك اختيار الإجراء المطلوب من القائمة الرئيسية:"
    )
    try:
        await callback.message.edit_text(
            cancel_text,
            reply_markup=get_main_menu_kb(),
            parse_mode="Markdown"
        )
    except Exception:
        await callback.message.answer(
            cancel_text,
            reply_markup=get_main_menu_kb(),
            parse_mode="Markdown"
        )

@base_router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        await message.answer(
            "ℹ️ **لا توجد عملية نشطة لإلغائها حالياً**\n\n"
            "يمكنك تصفح السوق أو العودة عبر /start.",
            reply_markup=get_main_menu_kb(),
            parse_mode="Markdown"
        )
        return
    await state.clear()
    await message.answer(
        "❌ **تم إلغاء العملية والعودة للقائمة الرئيسية**",
        reply_markup=get_main_menu_kb(),
        parse_mode="Markdown"
    )
