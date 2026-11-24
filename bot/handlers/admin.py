from decimal import Decimal
from datetime import datetime, timezone
import asyncio
import logging
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlalchemy.future import select
from sqlalchemy import func, or_

from db.models import User, Deal, Offer, SystemSetting, Dispute, DealRating, PaymentProof, AuditLog, Negotiation, KycRequest
from bot.states.flow_states import AdminFSM
from bot.middlewares.maintenance import set_cached_maintenance
from bot.utils.formatters import format_usdt, format_syp, safe_username, escape_markdown, format_local_datetime, PAYMENT_METHOD_ICONS
from bot.utils.deal_views import (
    format_admin_deal_review_text,
    build_admin_deal_keyboard,
    can_admin_recover_cancelled_deal,
)
from bot.utils.deal_card import push_deal_card
from bot.services.backup_service import perform_and_send_backup
from bot.services.rating_service import recalculate_user_reputation, format_stars
from bot.services.audit_service import record_audit, add_audit, LOG_FILE
from bot.services.channel import update_channel_offer_message, build_channel_offer_message
from bot.utils.offer_wizard import format_duration_hours_minutes
from bot.services.admin_notifier import dispatch_admin_notification, notify_usdt_deposit_confirmed
from bot.middlewares.error_alert import report_handled_exception
from config.settings import settings
from bot.utils.branding import BOT_NAME
from datetime import timedelta

logger = logging.getLogger(__name__)

admin_router = Router()

OPEN_DEAL_STATUSES = [
    "PENDING_OWNER_APPROVAL",
    "CREATED",
    "AWAITING_USDT_DEPOSIT",
    "USDT_DEPOSITED",
    "AWAITING_SYP_PAYMENT",
    "SYP_PAYMENT_SENT",
    "ADMIN_APPROVAL",
]


def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS


def get_admin_main_kb(maintenance_on: bool, pending_count: int = 0) -> InlineKeyboardMarkup:
    m_icon = "🟢 مفعّل" if maintenance_on else "🔴 معطّل"
    pending_badge = f" ({pending_count})" if pending_count > 0 else ""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=f"📡 العمليات الجارية والمعلقة{pending_badge}",
                callback_data="adm_live_ops"
            )
        ],
        [
            InlineKeyboardButton(text="🔍 استعلام عن عملية ربط", callback_data="admin_search_deal"),
            InlineKeyboardButton(text="👤 استعلام عن مستخدم", callback_data="admin_search_user")
        ],
        [
            InlineKeyboardButton(text="📊 التقارير المالية والسوق", callback_data="admin_stats"),
            InlineKeyboardButton(text="📋 سجل التدقيق والعمليات", callback_data="adm_open_audit")
        ],
        [
            InlineKeyboardButton(text="📢 رسالة بث جماعي", callback_data="admin_broadcast"),
            InlineKeyboardButton(text="💾 نسخ احتياطي فوري", callback_data="adm_trigger_backup")
        ],
        [
            InlineKeyboardButton(text=f"🛠️ وضع الصيانة: {m_icon}", callback_data="admin_toggle_maintenance")
        ],
        [
            InlineKeyboardButton(text="🔙 العودة للقائمة الرئيسية", callback_data="menu_back_main")
        ]
    ])


def get_admin_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 العودة للوحة الإدارة", callback_data="admin_main_menu")]
    ])


async def _build_admin_dashboard_view(db_session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    result = await db_session.execute(
        select(SystemSetting).where(SystemSetting.key == "maintenance_mode")
    )
    setting = result.scalar_one_or_none()
    is_m = False
    if setting:
        if isinstance(setting.value, dict):
            is_m = bool(setting.value.get("enabled", False))
        elif isinstance(setting.value, bool):
            is_m = setting.value
    set_cached_maintenance(is_m)

    pending_res = await db_session.execute(
        select(func.count(Deal.id)).where(Deal.status.in_(["ADMIN_APPROVAL", "SYP_PAYMENT_SENT"]))
    )
    pending_cnt = pending_res.scalar_one() or 0

    active_deals_res = await db_session.execute(
        select(func.count(Deal.id)).where(Deal.status.in_(OPEN_DEAL_STATUSES))
    )
    active_deals_cnt = active_deals_res.scalar_one() or 0

    active_offers_res = await db_session.execute(
        select(func.count(Offer.id)).where(Offer.status.in_(["active", "negotiating", "reserved"]))
    )
    active_offers_cnt = active_offers_res.scalar_one() or 0

    pending_notes_res = await db_session.execute(
        select(func.count(Offer.id)).where(Offer.status == "pending_approval")
    )
    pending_notes_cnt = pending_notes_res.scalar_one() or 0

    pending_kyc_res = await db_session.execute(
        select(func.count(KycRequest.id)).where(KycRequest.status == "pending")
    )
    pending_kyc_cnt = pending_kyc_res.scalar_one() or 0

    m_status_ar = "🟢 مفعل (الوصول محصور بالأدمن)" if is_m else "🔴 معطل (متاح لجميع المستخدمين)"

    text = (
        f"👑 **غرفة العمليات والإدارة — {BOT_NAME}**\n\n"
        f"📡 **نبض النظام اللحظي:**\n"
        f"▫️ **بانتظار الاعتماد أو التأكيد:** `{pending_cnt}` صفقة\n"
        f"▫️ **عروض بانتظار اعتماد الملاحظة:** `{pending_notes_cnt}` عرض\n"
        f"▫️ **طلبات توثيق الهوية المعلقة:** `{pending_kyc_cnt}` طلب\n"
        f"▫️ **إجمالي الصفقات الجارية الآن:** `{active_deals_cnt}` صفقة\n"
        f"▫️ **العروض النشطة في السوق:** `{active_offers_cnt}` عرض\n"
        f"▫️ **حالة الصيانة:** {m_status_ar}\n\n"
        "⚡ **الأوامر الإدارية السريعة:**\n"
        "• `/deal [رقم]` — فحص عملية ربط والتدخل الفوري\n"
        "• `/user [معرف/يوزر/لقب]` — كشف ملف مستخدم وإدارته\n"
        "• `/pending` — غرفة العمليات الجارية والمعلقة\n"
        "• `/backup` — سحب نسخة احتياطية مشفرة فوراً\n\n"
        "اختر القسم المطلوب من لوحة التحكم:"
    )
    return text, get_admin_main_kb(is_m, pending_cnt + pending_notes_cnt + pending_kyc_cnt)


# ================= 1. ADMIN MENU ENTRYPOINT =================

@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, db_session: AsyncSession, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    text, kb = await _build_admin_dashboard_view(db_session)
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@admin_router.callback_query(F.data == "admin_main_menu")
async def cq_admin_main_menu(callback: CallbackQuery, db_session: AsyncSession, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك بالوصول.", show_alert=True)
        return
    await state.clear()
    text, kb = await _build_admin_dashboard_view(db_session)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


# ================= 2. UNIFIED FINANCIAL & MARKET REPORTS =================

async def get_stats_text(db_session: AsyncSession) -> str:
    total_users_res = await db_session.execute(select(func.count()).select_from(User))
    total_users = total_users_res.scalar_one() or 0

    banned_users_res = await db_session.execute(
        select(func.count()).select_from(User).where(User.is_banned.is_(True))
    )
    banned_users = banned_users_res.scalar_one() or 0

    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    deals_all_res = await db_session.execute(
        select(Deal.status, func.count()).group_by(Deal.status)
    )
    deals_all_map = {status.upper() if status else "UNKNOWN": count for status, count in deals_all_res.all()}

    comp_all = deals_all_map.get("COMPLETED", 0)
    exp_all = deals_all_map.get("EXPIRED", 0)
    canc_all = deals_all_map.get("CANCELLED", 0)
    disp_all = deals_all_map.get("DISPUTED", 0)
    total_closed_all = comp_all + exp_all + canc_all + disp_all

    deals_month_res = await db_session.execute(
        select(Deal.status, func.count())
        .where(Deal.created_at >= month_start)
        .group_by(Deal.status)
    )
    deals_month_map = {status.upper() if status else "UNKNOWN": count for status, count in deals_month_res.all()}

    comp_month = deals_month_map.get("COMPLETED", 0)
    exp_month = deals_month_map.get("EXPIRED", 0)
    canc_month = deals_month_map.get("CANCELLED", 0)
    disp_month = deals_month_map.get("DISPUTED", 0)

    def calc_pct(count: int, total: int) -> str:
        if total <= 0:
            return "0.0%"
        return f"{(count / total * 100):.1f}%"

    vol_all_res = await db_session.execute(
        select(
            func.coalesce(func.sum(Deal.amount_usdt), 0),
            func.coalesce(func.sum(Deal.amount_syp), 0),
            func.coalesce(func.sum(Deal.fee_usdt), 0),
        ).where(func.upper(Deal.status) == "COMPLETED")
    )
    total_usdt_vol, total_syp_vol, total_net_profit = vol_all_res.one()

    vol_month_res = await db_session.execute(
        select(
            func.coalesce(func.sum(Deal.amount_usdt), 0),
            func.coalesce(func.sum(Deal.amount_syp), 0),
            func.coalesce(func.sum(Deal.fee_usdt), 0),
        ).where(func.upper(Deal.status) == "COMPLETED", Deal.created_at >= month_start)
    )
    month_usdt_vol, month_syp_vol, month_net_profit = vol_month_res.one()

    active_offers_res = await db_session.execute(
        select(
            Offer.offer_type,
            func.count(Offer.id),
            func.coalesce(func.sum(Offer.max_limit), 0),
            func.coalesce(func.avg(Offer.rate), 0)
        ).where(Offer.status.in_(["active", "negotiating"]))
        .group_by(Offer.offer_type)
    )
    active_summary = {
        t: (cnt, sum_max, avg_rate)
        for t, cnt, sum_max, avg_rate in active_offers_res.all()
    }

    sell_cnt, sell_usdt_pool, sell_avg_rate = active_summary.get("sell", (0, Decimal("0"), Decimal("0")))
    buy_cnt, buy_usdt_pool, buy_avg_rate = active_summary.get("buy", (0, Decimal("0"), Decimal("0")))

    avg_rate_all = (total_syp_vol / total_usdt_vol).quantize(Decimal("1")) if total_usdt_vol > 0 else Decimal("0")

    return (
        f"📊 **التقارير المالية وتحليلات السوق — {BOT_NAME}**\n\n"
        f"👥 **المستخدمون المسجلون:** `{total_users:,}` (محظور: `{banned_users}`)\n\n"
        "💰 **صافي أرباح العمولات (الصفقات المكتملة):**\n"
        f"▫️ **الإجمالي التراكمي:** `{format_usdt(total_net_profit)} USDT`\n"
        f"▫️ **أرباح الشهر الحالي:** `{format_usdt(month_net_profit)} USDT`\n\n"
        "💵 **حجم التداول المنفّذ (Completed Volume):**\n"
        f"▫️ **الإجمالي العام:** `{format_usdt(total_usdt_vol)} USDT` (`{format_syp(total_syp_vol)}` ل.س)\n"
        f"▫️ **حجم الشهر الحالي:** `{format_usdt(month_usdt_vol)} USDT` (`{format_syp(month_syp_vol)}` ل.س)\n"
        f"▫️ **متوسط سعر التنفيذ العام:** `{format_syp(avg_rate_all)}` ل.س\n\n"
        "📦 **سيولة السوق والعروض النشطة حالياً:**\n"
        f"▫️ **عروض البيع (`{sell_cnt}`):** سيولة `{format_usdt(sell_usdt_pool)} USDT` | متوسط `{format_syp(sell_avg_rate)}` ل.س\n"
        f"▫️ **عروض الشراء (`{buy_cnt}`):** طلب `{format_usdt(buy_usdt_pool)} USDT` | متوسط `{format_syp(buy_avg_rate)}` ل.س\n\n"
        "📅 **حركة الصفقات (الشهر الحالي / الإجمالي):**\n"
        f"▫️ **المكتملة:** `{comp_month}` / `{comp_all}` ({calc_pct(comp_all, total_closed_all)})\n"
        f"▫️ **الملغاة:** `{canc_month}` / `{canc_all}` ({calc_pct(canc_all, total_closed_all)})\n"
        f"▫️ **المنتهية:** `{exp_month}` / `{exp_all}` ({calc_pct(exp_all, total_closed_all)})\n"
        f"▫️ **عدم استلام (DISPUTED):** `{disp_month}` / `{disp_all}`"
    )


@admin_router.message(Command("stats"))
async def cmd_admin_stats(message: Message, db_session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    stats_text = await get_stats_text(db_session)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 تحديث التقرير المالي", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🔙 العودة للوحة الإدارة", callback_data="admin_main_menu")]
    ])
    await message.answer(stats_text, reply_markup=kb, parse_mode="Markdown")


@admin_router.callback_query(F.data == "admin_stats")
async def cq_admin_stats(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك بالوصول.", show_alert=True)
        return

    stats_text = await get_stats_text(db_session)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 تحديث التقرير المالي", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🔙 العودة للوحة الإدارة", callback_data="admin_main_menu")]
    ])

    try:
        await callback.message.edit_text(stats_text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(stats_text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


# ================= 3. COMPREHENSIVE USER LOOKUP & MANAGEMENT =================

async def _find_user_by_query(query_str: str, db_session: AsyncSession) -> User | None:
    cleaned = (query_str or "").strip()
    if not cleaned:
        return None
    cleaned_digits = cleaned.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    if cleaned_digits.isdigit():
        res = await db_session.execute(select(User).where(User.telegram_id == int(cleaned_digits)))
        return res.scalar_one_or_none()

    uname_clean = cleaned.lstrip("@")
    res = await db_session.execute(
        select(User).where(
            or_(
                func.lower(User.username) == uname_clean.lower(),
                func.lower(User.nickname) == cleaned.lower(),
            )
        ).order_by(User.created_at.desc()).limit(1)
    )
    return res.scalar_one_or_none()


@admin_router.message(Command("user", "search"))
async def cmd_admin_user_search(message: Message, state: FSMContext, db_session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) >= 2 and args[1].strip():
        await state.clear()
        query_val = args[1].strip()
        user = await _find_user_by_query(query_val, db_session)
        if not user:
            await message.answer(
                f"❌ لم يتم العثور على أي مستخدم مطابق لـ `{escape_markdown(query_val)}`.\n"
                "يمكنك البحث بالمعرف الرقمي، أو `@username`، أو اللقب.",
                reply_markup=get_admin_back_kb(),
                parse_mode="Markdown"
            )
            return
        await send_user_card(message, user, db_session)
        return

    await state.set_state(AdminFSM.waiting_for_user_search_id)
    text = (
        "👤 **الاستعلام الشامل عن مستخدم**\n\n"
        "أرسل أحد البيانات التالية للبحث عن المستخدم:\n"
        "▫️ **المعرف الرقمي (Telegram ID):** مثل `123456789`\n"
        "▫️ **اسم المستخدم (Username):** مثل `@username`\n"
        "▫️ **اللقب المسجل (Nickname)**"
    )
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_main_menu")]
    ])
    sent_msg = await message.answer(text, reply_markup=cancel_kb, parse_mode="Markdown")
    await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)


@admin_router.callback_query(F.data == "admin_search_user")
async def cq_admin_search_user(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك بالوصول.", show_alert=True)
        return

    await state.set_state(AdminFSM.waiting_for_user_search_id)
    text = (
        "👤 **الاستعلام الشامل عن مستخدم**\n\n"
        "أرسل أحد البيانات التالية للبحث عن المستخدم:\n"
        "▫️ **المعرف الرقمي (Telegram ID):** مثل `123456789`\n"
        "▫️ **اسم المستخدم (Username):** مثل `@username`\n"
        "▫️ **اللقب المسجل (Nickname)**"
    )
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_main_menu")]
    ])
    try:
        sent_msg = await callback.message.edit_text(text, reply_markup=cancel_kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    except Exception:
        sent_msg = await callback.message.answer(text, reply_markup=cancel_kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    await callback.answer()


@admin_router.message(AdminFSM.waiting_for_user_search_id)
async def process_user_search(message: Message, state: FSMContext, db_session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    text_input = (message.text or "").strip()
    if not text_input:
        await message.answer(
            "⚠️ يرجى إرسال المعرف الرقمي أو `@username` أو اللقب:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_main_menu")]
            ]),
            parse_mode="Markdown"
        )
        return

    user = await _find_user_by_query(text_input, db_session)
    await state.clear()

    if not user:
        await message.answer(
            f"❌ لم يتم العثور على أي مستخدم مسجل يطابق `{escape_markdown(text_input)}` في قاعدة البيانات.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔍 بحث مرة أخرى", callback_data="admin_search_user")],
                [InlineKeyboardButton(text="🔙 العودة للوحة الإدارة", callback_data="admin_main_menu")]
            ]),
            parse_mode="Markdown"
        )
        return

    await send_user_card(message, user, db_session)


@admin_router.callback_query(F.data.startswith("adm_view_user_"))
async def cq_admin_view_user(callback: CallbackQuery, db_session: AsyncSession, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك بالوصول.", show_alert=True)
        return
    await state.clear()

    target_id = int(callback.data.split("_")[-1])
    res = await db_session.execute(select(User).where(User.telegram_id == target_id))
    user = res.scalar_one_or_none()
    if not user:
        await callback.answer("المستخدم غير موجود في قاعدة البيانات.", show_alert=True)
        return

    await send_user_card(callback, user, db_session, is_edit=True)
    await callback.answer()


async def send_user_card(event_or_msg, user: User, db_session: AsyncSession, is_edit: bool = False):
    uid = user.telegram_id
    ban_badge = "🚫 محظور" if user.is_banned else "✅ نشط ومصرح"
    uname = safe_username(user.username, uid)
    nick_str = f"`{escape_markdown(user.nickname)}`" if getattr(user, "nickname", None) else "غير محدد"
    reg_date = format_local_datetime(user.created_at) if user.created_at else "غير محدد"

    # Saved payment accounts
    saved_usdt = getattr(user, "saved_usdt_address", None)
    usdt_wallet_str = f"`{escape_markdown(saved_usdt)}`" if saved_usdt else "غير محفوظة"

    saved_pm = getattr(user, "saved_payment_info", None)
    if isinstance(saved_pm, dict) and (saved_pm.get("info") or saved_pm.get("method")):
        pm_m = escape_markdown(str(saved_pm.get("method") or ""))
        pm_i = escape_markdown(str(saved_pm.get("info") or ""))
        syp_account_str = f"{pm_m} — `{pm_i}`" if pm_m else f"`{pm_i}`"
    elif isinstance(saved_pm, str) and saved_pm.strip():
        syp_account_str = f"`{escape_markdown(saved_pm.strip())}`"
    else:
        syp_account_str = "غير محفوظ"

    # Deal status counts for this user
    deals_stat_res = await db_session.execute(
        select(Deal.status, func.count(Deal.id))
        .where(or_(Deal.seller_id == uid, Deal.buyer_id == uid))
        .group_by(Deal.status)
    )
    d_map = {st: cnt for st, cnt in deals_stat_res.all()}
    active_deals_cnt = sum(d_map.get(st, 0) for st in OPEN_DEAL_STATUSES)
    comp_cnt = d_map.get("COMPLETED", 0)
    canc_cnt = d_map.get("CANCELLED", 0) + d_map.get("EXPIRED", 0)
    disp_cnt = d_map.get("DISPUTED", 0)

    # Total traded volume in completed deals
    vol_res = await db_session.execute(
        select(
            func.coalesce(func.sum(Deal.amount_usdt), 0),
            func.coalesce(func.sum(Deal.amount_syp), 0),
        ).where(
            or_(Deal.seller_id == uid, Deal.buyer_id == uid),
            Deal.status == "COMPLETED"
        )
    )
    user_vol_usdt, user_vol_syp = vol_res.one()

    # Active & pending-approval offers count
    offers_cnt_res = await db_session.execute(
        select(func.count(Offer.id)).where(
            Offer.user_id == uid,
            Offer.status.in_(["active", "pending_approval", "negotiating", "reserved"])
        )
    )
    active_offers_cnt = offers_cnt_res.scalar_one() or 0

    # Ratings received count
    ratings_cnt_res = await db_session.execute(
        select(func.count(DealRating.id)).where(DealRating.target_id == uid)
    )
    ratings_cnt = ratings_cnt_res.scalar_one() or 0

    rep_score = user.reputation_score if user.reputation_score is not None else Decimal("100.0")

    kyc_status_val = getattr(user, "kyc_status", "unverified") or "unverified"
    if kyc_status_val == "verified":
        kyc_badge = "🟢 موثق ✅"
    elif kyc_status_val == "pending":
        kyc_badge = "🟡 قيد المراجعة ⏳"
    elif kyc_status_val == "rejected":
        kyc_badge = "🔴 مرفوض ❌"
    else:
        kyc_badge = "⚪ غير موثق"

    latest_kyc_res = await db_session.execute(
        select(KycRequest)
        .where(KycRequest.user_id == uid)
        .order_by(KycRequest.id.desc())
        .limit(1)
    )
    latest_kyc = latest_kyc_res.scalar_one_or_none()

    kyc_details_block = ""
    if latest_kyc:
        safe_fn = escape_markdown(str(latest_kyc.full_name or ""))
        safe_nid = escape_markdown(str(latest_kyc.national_id or ""))
        safe_ph = escape_markdown(str(latest_kyc.phone_number or ""))
        kyc_details_block = (
            f"🪪 **بيانات توثيق الهوية (طلب #{latest_kyc.id}):**\n"
            f"▫️ **الاسم الثلاثي:** `{safe_fn}`\n"
            f"▫️ **الرقم الوطني:** `{safe_nid}`\n"
            f"▫️ **رقم الموبايل:** `{safe_ph}`\n\n"
        )

    card_text = (
        f"👤 **السجل الكامل للمستخدم —** `{uid}`\n\n"
        f"▫️ **المعرف والاسم:** {uname} (`{uid}`)\n"
        f"▫️ **اللقب المسجل:** {nick_str}\n"
        f"▫️ **حالة الحساب:** {ban_badge}\n"
        f"▫️ **توثيق الهوية:** {kyc_badge}\n"
        f"▫️ **تاريخ الانضمام:** `{reg_date}`\n\n"
        f"{kyc_details_block}"
        f"💳 **الحسابات المالية المحفوظة:**\n"
        f"▫️ **محفظة BEP-20:** {usdt_wallet_str}\n"
        f"▫️ **حساب الليرة:** {syp_account_str}\n\n"
        f"📊 **ملخص النشاط والتداول:**\n"
        f"▫️ **الصفقات الجارية الآن:** `{active_deals_cnt}` صفقة\n"
        f"▫️ **المكتملة:** `{comp_cnt}` | **الملغاة/المنتهية:** `{canc_cnt}`\n"
        f"▫️ **حالات عدم الاستلام (DISPUTED):** `{disp_cnt}`\n"
        f"▫️ **إجمالي حجم التداول:** `{format_usdt(user_vol_usdt)} USDT` (`{format_syp(user_vol_syp)}` ل.س)\n"
        f"▫️ **العروض النشطة حالياً:** `{active_offers_cnt}` عرض\n"
        f"▫️ **تقييم السمعة:** ⭐ `{rep_score}%` (من `{ratings_cnt}` تقييم)"
    )

    toggle_btn_text = "✅ فك الحظر عن المستخدم" if user.is_banned else "🚫 حظر المستخدم فوراً"
    action = "unban" if user.is_banned else "ban"

    kb_rows = [
        [
            InlineKeyboardButton(text=f"📋 صفقات المستخدم ({active_deals_cnt + comp_cnt + disp_cnt})", callback_data=f"adm_user_deals_{uid}"),
            InlineKeyboardButton(text=f"⭐ التقييمات ({ratings_cnt})", callback_data=f"adm_user_ratings_{uid}")
        ],
        [
            InlineKeyboardButton(text=f"📦 العروض النشطة ({active_offers_cnt})", callback_data=f"adm_user_offers_{uid}"),
            InlineKeyboardButton(text="✉️ إرسال رسالة مباشرة", callback_data=f"adm_msg_user_{uid}")
        ],
    ]
    if latest_kyc:
        kb_rows.append([
            InlineKeyboardButton(
                text=f"🪪 وثائق وصور الهوية (طلب #{latest_kyc.id})",
                callback_data=f"adm_user_kyc_{uid}"
            )
        ])
    kb_rows.extend([
        [
            InlineKeyboardButton(text=toggle_btn_text, callback_data=f"admin_toggle_ban_{action}_{uid}")
        ],
        [
            InlineKeyboardButton(text="🔍 بحث عن مستخدم آخر", callback_data="admin_search_user"),
            InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin_main_menu")
        ]
    ])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    if is_edit and isinstance(event_or_msg, CallbackQuery):
        try:
            await event_or_msg.message.edit_text(card_text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await event_or_msg.message.answer(card_text, reply_markup=kb, parse_mode="Markdown")
    elif isinstance(event_or_msg, CallbackQuery):
        await event_or_msg.message.answer(card_text, reply_markup=kb, parse_mode="Markdown")
    else:
        await event_or_msg.answer(card_text, reply_markup=kb, parse_mode="Markdown")


@admin_router.callback_query(F.data.startswith("admin_toggle_ban_"))
async def cq_admin_toggle_ban(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك بالوصول.", show_alert=True)
        return

    parts = callback.data.split("_")
    action = parts[3]  # 'ban' or 'unban'
    target_id = int(parts[4])

    result = await db_session.execute(
        select(User).where(User.telegram_id == target_id).with_for_update()
    )
    user = result.scalar_one_or_none()

    if not user:
        await callback.answer("المستخدم غير موجود.", show_alert=True)
        return

    if action == "ban":
        user.is_banned = True
        msg = f"🚫 تم حظر المستخدم {target_id} بنجاح."
    else:
        user.is_banned = False
        msg = f"✅ تم إلغاء حظر المستخدم {target_id} بنجاح."

    add_audit(
        session=db_session,
        action="BAN_USER" if action == "ban" else "UNBAN_USER",
        target_type="user",
        target_id=target_id,
        actor_id=callback.from_user.id,
        actor_type="ADMIN",
        details={"username": user.username, "action": action}
    )

    await db_session.commit()

    await callback.answer(msg, show_alert=True)
    await send_user_card(callback, user, db_session, is_edit=True)


# --- User Sub-screens: Ratings, Deals, Offers, Direct Message ---

@admin_router.callback_query(F.data.startswith("adm_user_ratings_"))
async def cq_admin_user_ratings(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    uid = int(callback.data.split("_")[-1])
    user_res = await db_session.execute(select(User).where(User.telegram_id == uid))
    user = user_res.scalar_one_or_none()
    if not user:
        await callback.answer("المستخدم غير موجود.", show_alert=True)
        return

    ratings_res = await db_session.execute(
        select(DealRating)
        .where(DealRating.target_id == uid)
        .order_by(DealRating.id.desc())
        .limit(15)
    )
    ratings = list(ratings_res.scalars().all())

    rep_score = user.reputation_score if user.reputation_score is not None else Decimal("100.0")
    lines = [
        f"⭐ **سجل التقييمات المستلمة للمستخدم** `{uid}`\n"
        f"▫️ **تقييم السمعة الحالي:** `{rep_score}%`\n\n"
    ]

    kb_rows = []
    if not ratings:
        lines.append("لا توجد أي تقييمات مسجلة لهذا المستخدم حتى الآن.")
    else:
        lines.append("يمكنك حذف أي تقييم كيدي أو خاطئ بضغطة زر لإعادة احتساب السمعة فوراً:\n\n")
        for r in ratings:
            stars_vis = format_stars(r.stars)
            dt_str = format_local_datetime(r.created_at) if r.created_at else "—"
            lines.append(
                f"🔹 **صفقة #{r.deal_id}** — {stars_vis} (`{r.stars}/5`)\n"
                f"   • المقيم: `{r.rater_id}` | التوقيت: `{dt_str}`\n"
            )
            kb_rows.append([
                InlineKeyboardButton(
                    text=f"🗑️ حذف تقييم صفقة #{r.deal_id} ({r.stars}⭐)",
                    callback_data=f"adm_del_user_rating_{r.id}_{uid}"
                ),
                InlineKeyboardButton(
                    text=f"🔍 صفقة #{r.deal_id}",
                    callback_data=f"adm_inspect_deal_{r.deal_id}"
                )
            ])

    kb_rows.append([InlineKeyboardButton(text="🔙 العودة لملف المستخدم", callback_data=f"adm_view_user_{uid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    try:
        await callback.message.edit_text("".join(lines), reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer("".join(lines), reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm_del_user_rating_"))
async def cq_admin_del_user_rating(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    parts = callback.data.split("_")
    rating_id = int(parts[4])

    rating_res = await db_session.execute(
        select(DealRating).where(DealRating.id == rating_id).with_for_update()
    )
    rating = rating_res.scalar_one_or_none()
    if not rating:
        await callback.answer("التقييم غير موجود أو تم حذفه مسبقاً.", show_alert=True)
        return

    deal_id = rating.deal_id
    rater_id = rating.rater_id
    target_id = rating.target_id
    deleted_stars = rating.stars

    await db_session.delete(rating)
    await db_session.flush()

    new_rep, total_ratings = await recalculate_user_reputation(db_session, target_id)

    add_audit(
        session=db_session,
        action="DELETE_RATING",
        target_type="user",
        target_id=target_id,
        actor_id=callback.from_user.id,
        actor_type="ADMIN",
        details={
            "rating_id": rating_id,
            "deal_id": deal_id,
            "rater_id": rater_id,
            "deleted_stars": deleted_stars,
            "new_reputation_score": str(new_rep),
            "remaining_ratings": total_ratings
        }
    )
    await db_session.commit()

    await callback.answer(
        f"✅ تم حذف التقييم وإعادة احتساب سمعة المستخدم ({new_rep}%).",
        show_alert=True
    )
    # Refresh the user ratings view
    await cq_admin_user_ratings(callback, db_session)


@admin_router.callback_query(F.data.startswith("adm_user_deals_"))
async def cq_admin_user_deals(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    uid = int(callback.data.split("_")[-1])
    deals_res = await db_session.execute(
        select(Deal)
        .where(or_(Deal.seller_id == uid, Deal.buyer_id == uid))
        .order_by(Deal.id.desc())
        .limit(12)
    )
    deals = list(deals_res.scalars().all())

    lines = [
        f"📋 **سجل صفقات المستخدم** `{uid}` *(آخر 12 صفقة)*\n\n"
    ]
    kb_rows = []

    if not deals:
        lines.append("لا توجد أي صفقات مسجلة لهذا المستخدم.")
    else:
        for d in deals:
            role_ar = "🔴 بائع" if d.seller_id == uid else "🟢 مشتري"
            other_id = d.buyer_id if d.seller_id == uid else d.seller_id
            lines.append(
                f"🔸 **صفقة #{d.id}** — `{d.status}`\n"
                f"   • الدور: {role_ar} | الطرف الآخر: `{other_id}`\n"
                f"   • الكمية: `{format_usdt(d.amount_usdt)} USDT` (`{format_syp(d.amount_syp)}` ل.س)\n"
            )
            kb_rows.append([
                InlineKeyboardButton(
                    text=f"🔍 فحص الصفقة #{d.id} ({d.status})",
                    callback_data=f"adm_inspect_deal_{d.id}"
                )
            ])

    kb_rows.append([InlineKeyboardButton(text="🔙 العودة لملف المستخدم", callback_data=f"adm_view_user_{uid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    try:
        await callback.message.edit_text("".join(lines), reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer("".join(lines), reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm_user_offers_"))
async def cq_admin_user_offers(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    uid = int(callback.data.split("_")[-1])
    offers_res = await db_session.execute(
        select(Offer)
        .where(Offer.user_id == uid, Offer.status.in_(["active", "pending_approval", "negotiating", "reserved"]))
        .order_by(Offer.id.desc())
    )
    offers = list(offers_res.scalars().all())

    lines = [
        f"📦 **العروض النشطة والمعلقة للمستخدم** `{uid}`\n\n"
    ]
    kb_rows = []

    if not offers:
        lines.append("لا توجد أي عروض نشطة أو معلقة أو محجوزة لهذا المستخدم حالياً.")
    else:
        for o in offers:
            t_ar = "🔴 بيع USDT" if o.offer_type == "sell" else "🟢 شراء USDT"
            status_badge = "⏳ بانتظار موافقة ملاحظة (`pending_approval`)" if o.status == "pending_approval" else f"`{o.status}`"
            note_line = f"   • 📝 الملاحظة: `{escape_markdown(str(o.note))}`\n" if o.note else ""
            lines.append(
                f"🔹 **عرض #{o.id}** ({t_ar}) — الحالة: {status_badge}\n"
                f"   • الكمية: `{format_usdt(o.min_limit)}` - `{format_usdt(o.max_limit)} USDT` | السعر: `{format_syp(o.rate)}` ل.س\n"
                f"{note_line}"
            )
            if o.status == "pending_approval":
                kb_rows.append([
                    InlineKeyboardButton(
                        text=f"✅ موافقة ونشر #{o.id}",
                        callback_data=f"adm_note_app_{o.id}"
                    ),
                    InlineKeyboardButton(
                        text=f"❌ رفض مع السبب #{o.id}",
                        callback_data=f"adm_note_rej_{o.id}"
                    ),
                ])
            if o.status in ["active", "pending_approval", "negotiating"]:
                kb_rows.append([
                    InlineKeyboardButton(
                        text=f"🗑️ حذف وإلغاء العرض #{o.id} من السوق",
                        callback_data=f"adm_del_offer_{o.id}_{uid}"
                    )
                ])

    kb_rows.append([InlineKeyboardButton(text="🔙 العودة لملف المستخدم", callback_data=f"adm_view_user_{uid}")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    try:
        await callback.message.edit_text("".join(lines), reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer("".join(lines), reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm_del_offer_"))
async def cq_admin_delete_offer(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    parts = callback.data.split("_")
    offer_id = int(parts[3])
    return_uid = int(parts[4]) if len(parts) > 4 and parts[4].isdigit() else 0

    offer_res = await db_session.execute(
        select(Offer).where(Offer.id == offer_id).with_for_update()
    )
    offer = offer_res.scalar_one_or_none()
    if not offer:
        await callback.answer("العرض غير موجود.", show_alert=True)
        return

    if offer.status == "reserved":
        await callback.answer("⚠️ لا يمكن حذف العرض لأنه محجوز في صفقة جارية حالياً. قم بإلغاء الصفقة أولاً.", show_alert=True)
        return

    owner_id = offer.user_id
    offer.status = "deleted"
    offer.status_changed_at = datetime.now(timezone.utc)

    # Close any open negotiations on this offer so they never remain dangling
    open_negs_res = await db_session.execute(
        select(Negotiation)
        .where(
            Negotiation.offer_id == offer_id,
            Negotiation.status.in_(["PENDING_MAKER", "PENDING_TAKER"])
        )
        .with_for_update()
    )
    open_negs = list(open_negs_res.scalars().all())
    for neg in open_negs:
        neg.status = "REJECTED"

    add_audit(
        session=db_session,
        action="ADMIN_DELETE_OFFER",
        target_type="offer",
        target_id=offer_id,
        actor_id=callback.from_user.id,
        actor_type="ADMIN",
        details={"owner_id": owner_id, "closed_negotiations": [n.id for n in open_negs]}
    )
    await db_session.commit()

    await update_channel_offer_message(callback.bot, offer)

    try:
        await callback.bot.send_message(
            chat_id=owner_id,
            text=(
                f"🗑️ **إشعار إداري — حذف عرض #{offer_id}**\n\n"
                f"قامت إدارة {BOT_NAME} بإزالة عرضك رقم `#{offer_id}` من السوق والقناة العامة."
            ),
            parse_mode="Markdown"
        )
    except Exception:
        pass

    for neg in open_negs:
        try:
            await callback.bot.send_message(
                chat_id=neg.taker_id,
                text=(
                    f"ℹ️ **إغلاق جلسة التفاوض — عرض #{offer_id}**\n\n"
                    f"تم إغلاق جلسة التفاوض رقم `#{neg.id}` نظراً لإزالة العرض من السوق من قِبل الإدارة."
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass

    await callback.answer(f"✅ تم حذف العرض #{offer_id} وإزالته من القناة العامة.", show_alert=True)

    if return_uid > 0:
        callback_copy = callback.model_copy(update={"data": f"adm_user_offers_{return_uid}"})
        await cq_admin_user_offers(callback_copy, db_session)
    else:
        callback_copy = callback.model_copy(update={"data": "adm_ops_filter_offers"})
        await cq_admin_live_ops_filter(callback_copy, db_session)


@admin_router.callback_query(F.data.startswith("adm_msg_user_"))
async def cq_admin_msg_user(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    uid = int(callback.data.split("_")[-1])
    await state.set_state(AdminFSM.waiting_for_direct_user_msg)
    await state.update_data(target_user_id=uid)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء والعودة لملف المستخدم", callback_data=f"adm_view_user_{uid}")]
    ])
    text = (
        f"✉️ **إرسال رسالة إدارية مباشرة إلى المستخدم** `{uid}`\n\n"
        f"أرسل الآن نص الرسالة المطلوب إرسالها إلى هذا المستخدم عبر {BOT_NAME}:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@admin_router.message(AdminFSM.waiting_for_direct_user_msg)
async def process_direct_user_msg(message: Message, state: FSMContext, db_session: AsyncSession, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    target_uid = data.get("target_user_id")
    msg_content = (message.text or message.caption or "").strip()

    if not msg_content or not target_uid:
        await message.answer("⚠️ يرجى إرسال نص واضح للرسالة.")
        return

    await state.clear()

    formatted_msg = (
        f"📩 **رسالة رسمية من إدارة {BOT_NAME}**\n\n"
        f"{escape_markdown(msg_content)}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 العودة لملف المستخدم", callback_data=f"adm_view_user_{target_uid}")],
        [InlineKeyboardButton(text="👑 لوحة الإدارة", callback_data="admin_main_menu")]
    ])

    try:
        await bot.send_message(chat_id=target_uid, text=formatted_msg, parse_mode="Markdown")
        await message.answer(
            f"✅ **تم إرسال الرسالة الإدارية بنجاح إلى المستخدم** `{target_uid}`.",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    except Exception as e:
        await message.answer(
            f"❌ **تعذر إرسال الرسالة إلى المستخدم** `{target_uid}`:\n`{e}`",
            reply_markup=kb,
            parse_mode="Markdown"
        )


# ================= 4. LIVE OPERATIONS CENTER (/pending & adm_live_ops) =================

def _build_live_ops_filter_kb(active_tab: str) -> list[list[InlineKeyboardButton]]:
    tabs = [
        ("pending", "🛡️ بانتظار الاعتماد/التأكيد"),
        ("active", "⏳ جميع الصفقات الجارية"),
        ("disputed", "⚠️ صفقات عدم الاستلام"),
        ("offers", "📦 العروض النشطة بالسوق"),
    ]
    rows = []
    pair = []
    for key, label in tabs:
        prefix = "🔘 " if key == active_tab else ""
        pair.append(InlineKeyboardButton(text=f"{prefix}{label}", callback_data=f"adm_ops_filter_{key}"))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    return rows


async def _render_live_ops_center(
    db_session: AsyncSession,
    filter_key: str = "pending"
) -> tuple[str, InlineKeyboardMarkup]:
    nav_rows = _build_live_ops_filter_kb(filter_key)
    item_rows = []

    if filter_key == "pending":
        app_cnt_res = await db_session.execute(
            select(func.count(Deal.id)).where(Deal.status == "ADMIN_APPROVAL")
        )
        app_cnt = app_cnt_res.scalar() or 0
        syp_cnt_res = await db_session.execute(
            select(func.count(Deal.id)).where(Deal.status == "SYP_PAYMENT_SENT")
        )
        syp_cnt = syp_cnt_res.scalar() or 0
        notes_cnt_res = await db_session.execute(
            select(func.count(Offer.id)).where(Offer.status == "pending_approval")
        )
        notes_cnt = notes_cnt_res.scalar() or 0
        kyc_cnt_res = await db_session.execute(
            select(func.count(KycRequest.id)).where(KycRequest.status == "pending")
        )
        kyc_cnt = kyc_cnt_res.scalar() or 0
        total_pending = app_cnt + syp_cnt + notes_cnt + kyc_cnt

        res = await db_session.execute(
            select(Deal)
            .where(Deal.status.in_(["ADMIN_APPROVAL", "SYP_PAYMENT_SENT"]))
            .order_by(Deal.created_at.desc())
            .limit(10)
        )
        deals = list(res.scalars().all())

        pending_offers_res = await db_session.execute(
            select(Offer)
            .where(Offer.status == "pending_approval")
            .order_by(Offer.id.desc())
            .limit(10)
        )
        pending_offers = list(pending_offers_res.scalars().all())

        pending_kyc_res = await db_session.execute(
            select(KycRequest)
            .where(KycRequest.status == "pending")
            .order_by(KycRequest.id.desc())
            .limit(10)
        )
        pending_kyc_list = list(pending_kyc_res.scalars().all())

        lines = [
            f"📡 **غرفة العمليات — بانتظار الاعتماد والتأكيد ({total_pending})**\n"
            f"▫️ **بانتظار إطلاق الضمان (`ADMIN_APPROVAL`):** `{app_cnt}`\n"
            f"▫️ **بانتظار تأكيد استلام الليرة (`SYP_PAYMENT_SENT`):** `{syp_cnt}`\n"
            f"▫️ **عروض بانتظار اعتماد الملاحظة (`pending_approval`):** `{notes_cnt}`\n"
            f"▫️ **طلبات توثيق الهوية (`pending`):** `{kyc_cnt}`\n\n"
        ]
        if not deals and not pending_offers and not pending_kyc_list:
            lines.append("✅ لا توجد أي صفقات أو عروض أو طلبات توثيق معلقة بانتظار التدخل حالياً.")
        else:
            for d in deals:
                badge = "🛡️ إطلاق ضمان" if d.status == "ADMIN_APPROVAL" else "💸 تأكيد ليرة"
                lines.append(
                    f"🔸 **صفقة #{d.id}** ({badge})\n"
                    f"   • الكمية: `{format_usdt(d.amount_usdt)} USDT` | المقابل: `{format_syp(d.amount_syp)}` ل.س\n"
                    f"   • البائع: `{d.seller_id}` | المشتري: `{d.buyer_id}`\n"
                )
                item_rows.append([
                    InlineKeyboardButton(
                        text=f"🔍 فتح وفحص صفقة #{d.id} ({badge})",
                        callback_data=f"adm_inspect_deal_{d.id}"
                    )
                ])
            for o in pending_offers:
                t_ar = "🔴 بيع" if o.offer_type == "sell" else "🟢 شراء"
                safe_note = escape_markdown(str(o.note or ""))
                lines.append(
                    f"📝 **عرض #{o.id}** ({t_ar} — ملاحظة مخصصة) | التاجر: `{o.user_id}`\n"
                    f"   • الكمية: `{format_usdt(o.min_limit)}` - `{format_usdt(o.max_limit)} USDT` | السعر: `{format_syp(o.rate)}` ل.س\n"
                    f"   • الملاحظة: `{safe_note}`\n"
                )
                item_rows.append([
                    InlineKeyboardButton(
                        text=f"✅ موافقة ونشر #{o.id}",
                        callback_data=f"adm_note_app_{o.id}"
                    ),
                    InlineKeyboardButton(
                        text=f"❌ رفض مع السبب #{o.id}",
                        callback_data=f"adm_note_rej_{o.id}"
                    ),
                ])
            for k in pending_kyc_list:
                safe_fn = escape_markdown(str(k.full_name or ""))
                safe_nid = escape_markdown(str(k.national_id or ""))
                safe_ph = escape_markdown(str(k.phone_number or ""))
                lines.append(
                    f"🪪 **توثيق هوية #{k.id}** | المستخدم: `{k.user_id}`\n"
                    f"   • الاسم: `{safe_fn}` | الوطني: `{safe_nid}` | الموبايل: `{safe_ph}`\n"
                )
                item_rows.append([
                    InlineKeyboardButton(
                        text=f"🪪 عرض صور وبيانات التوثيق #{k.id}",
                        callback_data=f"adm_kyc_view_{k.id}"
                    ),
                    InlineKeyboardButton(
                        text=f"✅ قبول #{k.id}",
                        callback_data=f"adm_kyc_app_{k.id}"
                    ),
                    InlineKeyboardButton(
                        text=f"❌ رفض #{k.id}",
                        callback_data=f"adm_kyc_rej_{k.id}"
                    ),
                ])

    elif filter_key == "active":
        total_active_res = await db_session.execute(
            select(func.count(Deal.id)).where(Deal.status.in_(OPEN_DEAL_STATUSES))
        )
        total_active = total_active_res.scalar() or 0

        res = await db_session.execute(
            select(Deal)
            .where(Deal.status.in_(OPEN_DEAL_STATUSES))
            .order_by(Deal.id.desc())
            .limit(12)
        )
        deals = list(res.scalars().all())
        lines = [
            f"⏳ **غرفة العمليات — جميع الصفقات الجارية الآن ({total_active})**\n\n"
        ]
        if not deals:
            lines.append("لا توجد أي صفقات جارية أو مفتوحة في النظام حالياً.")
        else:
            for d in deals:
                lines.append(
                    f"🔸 **صفقة #{d.id}** — `{d.status}`\n"
                    f"   • الكمية: `{format_usdt(d.amount_usdt)} USDT` (`{format_syp(d.amount_syp)}` ل.س)\n"
                    f"   • البائع: `{d.seller_id}` | المشتري: `{d.buyer_id}`\n"
                )
                item_rows.append([
                    InlineKeyboardButton(
                        text=f"🔍 فحص والتدخل في صفقة #{d.id} ({d.status})",
                        callback_data=f"adm_inspect_deal_{d.id}"
                    )
                ])

    elif filter_key == "disputed":
        res = await db_session.execute(
            select(Deal)
            .where(Deal.status == "DISPUTED")
            .order_by(Deal.id.desc())
            .limit(12)
        )
        deals = list(res.scalars().all())
        lines = [
            f"⚠️ **غرفة العمليات — صفقات عدم الاستلام (`DISPUTED`) ({len(deals)})**\n\n"
        ]
        if not deals:
            lines.append("✅ لا توجد صفقات مسجلة بحالة عدم استلام.")
        else:
            for d in deals:
                lines.append(
                    f"🔸 **صفقة #{d.id}** — `{format_usdt(d.amount_usdt)} USDT` (`{format_syp(d.amount_syp)}` ل.س)\n"
                    f"   • البائع: `{d.seller_id}` | المشتري: `{d.buyer_id}`\n"
                )
                item_rows.append([
                    InlineKeyboardButton(
                        text=f"🔍 مراجعة ملف صفقة #{d.id}",
                        callback_data=f"adm_inspect_deal_{d.id}"
                    )
                ])

    else:  # offers
        res = await db_session.execute(
            select(Offer)
            .where(Offer.status.in_(["active", "pending_approval", "negotiating", "reserved"]))
            .order_by(Offer.id.desc())
            .limit(12)
        )
        offers = list(res.scalars().all())
        lines = [
            f"📦 **غرفة العمليات — العروض النشطة والمعلقة في السوق ({len(offers)})**\n\n"
        ]
        if not offers:
            lines.append("لا توجد عروض نشطة أو معلقة في السوق حالياً.")
        else:
            for o in offers:
                t_ar = "🔴 بيع" if o.offer_type == "sell" else "🟢 شراء"
                status_label = "⏳ بانتظار موافقة ملاحظة" if o.status == "pending_approval" else f"`{o.status}`"
                note_line = f"   • 📝 الملاحظة: `{escape_markdown(str(o.note))}`\n" if o.note else ""
                lines.append(
                    f"🔹 **عرض #{o.id}** ({t_ar}) — {status_label} | التاجر: `{o.user_id}`\n"
                    f"   • الكمية: `{format_usdt(o.min_limit)}` - `{format_usdt(o.max_limit)} USDT` | السعر: `{format_syp(o.rate)}` ل.س\n"
                    f"{note_line}"
                )
                if o.status == "pending_approval":
                    item_rows.append([
                        InlineKeyboardButton(text=f"✅ موافقة ونشر #{o.id}", callback_data=f"adm_note_app_{o.id}"),
                        InlineKeyboardButton(text=f"❌ رفض مع السبب #{o.id}", callback_data=f"adm_note_rej_{o.id}"),
                    ])
                row_btns = [
                    InlineKeyboardButton(text=f"👤 التاجر {o.user_id}", callback_data=f"adm_view_user_{o.user_id}")
                ]
                if o.status in ["active", "pending_approval", "negotiating"]:
                    row_btns.append(
                        InlineKeyboardButton(text=f"🗑️ حذف عرض #{o.id}", callback_data=f"adm_del_offer_{o.id}_0")
                    )
                item_rows.append(row_btns)

    footer_rows = [
        [
            InlineKeyboardButton(text="🔄 تحديث القائمة", callback_data=f"adm_ops_filter_{filter_key}"),
            InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin_main_menu")
        ]
    ]
    kb = InlineKeyboardMarkup(inline_keyboard=nav_rows + item_rows + footer_rows)
    return "".join(lines), kb


@admin_router.message(Command("pending"))
async def cmd_admin_pending(message: Message, db_session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    text, kb = await _render_live_ops_center(db_session, "pending")
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@admin_router.callback_query(F.data == "adm_live_ops")
async def cq_admin_live_ops(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return
    text, kb = await _render_live_ops_center(db_session, "pending")
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm_ops_filter_"))
async def cq_admin_live_ops_filter(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return
    filter_key = callback.data.split("_")[-1]
    text, kb = await _render_live_ops_center(db_session, filter_key)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


# ================= 5. DEAL INSPECTION & ADMIN INTERVENTION (/deal) =================

@admin_router.callback_query(F.data == "admin_search_deal")
async def cq_admin_search_deal(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك بالوصول.", show_alert=True)
        return

    await state.set_state(AdminFSM.waiting_for_deal_search_id)
    text = (
        "🔍 **الاستعلام عن صفقة / عملية ربط**\n\n"
        "أرسل رقم الصفقة أو عملية الربط المطلوب فحصها والتدخل بها (أرقام فقط):"
    )
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_main_menu")]
    ])
    try:
        sent_msg = await callback.message.edit_text(text, reply_markup=cancel_kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    except Exception:
        sent_msg = await callback.message.answer(text, reply_markup=cancel_kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    await callback.answer()


@admin_router.message(AdminFSM.waiting_for_deal_search_id)
async def process_deal_search(message: Message, state: FSMContext, db_session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    raw_input = (message.text or "").strip().lstrip("#")
    raw_input = raw_input.translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    if not raw_input.isdigit():
        warn_msg = await message.answer(
            "⚠️ **رقم غير صالح!** يرجى إرسال رقم الصفقة / عملية الربط بالأرقام فقط:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_main_menu")]
            ]),
            parse_mode="Markdown"
        )
        await state.update_data(prompt_msg_id=warn_msg.message_id, prompt_chat_id=warn_msg.chat.id)
        return

    deal_id = int(raw_input)
    result = await db_session.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()

    await state.clear()
    if not deal:
        await message.answer(
            f"❌ لم يتم العثور على أي صفقة بالمعرف `#{deal_id}`.",
            reply_markup=get_admin_back_kb(),
            parse_mode="Markdown"
        )
        return

    admin_text = await format_admin_deal_review_text(deal, db_session)
    kb = await build_admin_deal_keyboard(deal, db_session)
    await message.answer(admin_text, reply_markup=kb, parse_mode="Markdown")


@admin_router.message(Command("deal"))
async def cmd_admin_inspect_deal(message: Message, state: FSMContext, db_session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    args = (message.text or "").split()
    if len(args) < 2:
        await state.set_state(AdminFSM.waiting_for_deal_search_id)
        cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_main_menu")]
        ])
        sent_msg = await message.answer(
            "🔍 **الاستعلام عن صفقة / عملية ربط**\n\n"
            "أرسل رقم الصفقة أو عملية الربط المطلوب فحصها (أرقام فقط):",
            reply_markup=cancel_kb,
            parse_mode="Markdown"
        )
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
        return

    deal_arg = args[1].lstrip("#").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    if not deal_arg.isdigit():
        await message.answer(
            "⚠️ **طريقة الاستخدام:**\n`/deal [رقم_الصفقة]`\n\n*(مثال: `/deal 15`)*",
            parse_mode="Markdown"
        )
        return

    await state.clear()
    deal_id = int(deal_arg)
    result = await db_session.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()

    if not deal:
        await message.answer(f"❌ لم يتم العثور على أي صفقة بالمعرف `#{deal_id}`.", parse_mode="Markdown")
        return

    admin_text = await format_admin_deal_review_text(deal, db_session)
    kb = await build_admin_deal_keyboard(deal, db_session)
    await message.answer(admin_text, reply_markup=kb, parse_mode="Markdown")


@admin_router.callback_query(F.data.startswith("adm_inspect_deal_"))
async def cq_admin_inspect_deal(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك بالوصول.", show_alert=True)
        return

    deal_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()

    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return

    admin_text = await format_admin_deal_review_text(deal, db_session)
    kb = await build_admin_deal_keyboard(deal, db_session)

    try:
        await callback.message.edit_text(admin_text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(admin_text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


# --- View Payment Proof Photo ---

@admin_router.callback_query(F.data.startswith("adm_view_proof_"))
async def cq_admin_view_proof(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    deal_id = int(callback.data.split("_")[-1])
    proof_res = await db_session.execute(
        select(PaymentProof)
        .where(PaymentProof.deal_id == deal_id)
        .order_by(PaymentProof.id.desc())
        .limit(1)
    )
    proof = proof_res.scalar_one_or_none()
    if not proof or not proof.transaction_reference:
        await callback.answer("لا يوجد إشعار دفع مرفق لهذه الصفقة.", show_alert=True)
        return

    ref = proof.transaction_reference
    file_id = ref.removeprefix("PHOTO_") if ref.startswith("PHOTO_") else ref
    proof_dt = getattr(proof, "submitted_at", None)
    dt_str = format_local_datetime(proof_dt) if proof_dt else "—"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"🔍 العودة لتفاصيل الصفقة #{deal_id}", callback_data=f"adm_inspect_deal_{deal_id}")]
    ])
    try:
        await callback.message.answer_photo(
            photo=file_id,
            caption=(
                f"📸 **صورة إشعار تحويل الليرة — صفقة #{deal_id}**\n"
                f"▫️ **توقيت الإرفاق:** `{dt_str}`"
            ),
            reply_markup=kb,
            parse_mode="Markdown"
        )
        await callback.answer()
    except Exception as e:
        await callback.answer(f"تعذر عرض الصورة: {e}", show_alert=True)


# --- Admin Deal Operational Interventions (Manual Deposit / Force Confirm SYP / Force Dispute / Force Cancel) ---

@admin_router.callback_query(F.data.startswith("adm_ask_manual_dep_"))
async def cq_admin_ask_manual_deposit(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    deal_id = int(callback.data.split("_")[-1])
    res = await db_session.execute(select(Deal).where(Deal.id == deal_id))
    deal = res.scalar_one_or_none()
    if not deal:
        await callback.answer("الصفقة غير موجودة.", show_alert=True)
        return

    if not (deal.deposit_address or "").strip():
        await callback.answer("⚠️ لا يوجد عنوان محفظة إيداع مرتبط بهذه الصفقة.", show_alert=True)
        return

    if deal.status in ("CANCELLED", "EXPIRED"):
        can_recover, reason = await can_admin_recover_cancelled_deal(deal, db_session)
        if not can_recover:
            await callback.answer(reason, show_alert=True)
            return

    seller_fee = getattr(deal, "seller_fee_usdt", None) or Decimal("0")
    expected_dep = format_usdt(deal.amount_usdt + seller_fee)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ نعم، تأكيد الإيداع يدوياً", callback_data=f"adm_do_manual_dep_{deal_id}"),
            InlineKeyboardButton(text="↩️ تراجع", callback_data=f"adm_inspect_deal_{deal_id}")
        ]
    ])
    text = (
        f"✅ **تأكيد إيداع الـ USDT يدوياً (طوارئ API) — صفقة #{deal_id}**\n\n"
        f"▫️ **المبلغ المطلوب التحقق منه:** `{expected_dep} USDT`\n"
        f"▫️ **عنوان محفظة الضمان (`#{deal.deposit_index}`):**\n`{escape_markdown(deal.deposit_address)}`\n\n"
        "هل تحققت من وصول رصيد الضمان إلى المحفظة أعلاه وترغب بنقل الصفقة لمرحلة تحويل الليرة السورية؟"
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm_do_manual_dep_"))
async def cq_admin_do_manual_deposit(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    deal_id = int(callback.data.split("_")[-1])
    res = await db_session.execute(select(Deal).where(Deal.id == deal_id).with_for_update())
    deal = res.scalar_one_or_none()
    if not deal:
        await callback.answer("الصفقة غير موجودة.", show_alert=True)
        return

    if deal.status not in ("CREATED", "AWAITING_USDT_DEPOSIT", "CANCELLED", "EXPIRED"):
        await callback.answer(f"لا يمكن تنفيذ هذا الإجراء، حالة الصفقة الحالية: {deal.status}", show_alert=True)
        return

    if not (deal.deposit_address or "").strip():
        await callback.answer("⚠️ لا يوجد عنوان محفظة إيداع مرتبط بهذه الصفقة.", show_alert=True)
        return

    was_closed = deal.status in ("CANCELLED", "EXPIRED")
    recovered_offer = None
    offer = None

    if deal.offer_id:
        offer_res = await db_session.execute(
            select(Offer).where(Offer.id == deal.offer_id).with_for_update()
        )
        offer = offer_res.scalar_one_or_none()

    if was_closed:
        can_recover, reason = await can_admin_recover_cancelled_deal(deal, db_session)
        if not can_recover:
            await db_session.commit()
            await callback.answer(reason, show_alert=True)
            return

        if offer:
            if offer.status == "completed":
                await db_session.commit()
                await callback.answer(
                    "⚠️ لا يمكن استعادة الصفقة لأن العرض المرتبط بها قد اكتمل مسبقاً.",
                    show_alert=True
                )
                return
            if offer.status == "negotiating":
                await db_session.commit()
                await callback.answer(
                    "⚠️ لا يمكن استعادة الصفقة لأن العرض مرتبط حالياً بجلسة تفاوض جارية.",
                    show_alert=True
                )
                return
            if offer.status in ("active", "expired", "cancelled", "deleted"):
                offer.status = "reserved"
                offer.status_changed_at = datetime.now(timezone.utc)
                recovered_offer = offer

    seller_method_val = ""
    seller_info_val = ""
    if isinstance(deal.seller_payment_info, dict):
        seller_method_val = str(deal.seller_payment_info.get("method") or "").strip()
        seller_info_val = str(deal.seller_payment_info.get("info") or "").strip()
    elif isinstance(deal.seller_payment_info, str):
        seller_info_val = deal.seller_payment_info.strip()

    now = datetime.now(timezone.utc)
    if seller_info_val and seller_info_val not in ("PENDING", "غير محدد", "N/A"):
        deal.status = "AWAITING_SYP_PAYMENT"
        deal.expires_at = now + timedelta(minutes=30)
    else:
        deal.status = "USDT_DEPOSITED"
        deal.expires_at = now + timedelta(minutes=30)

    add_audit(
        session=db_session,
        action="ADMIN_MANUAL_CONFIRM_DEPOSIT",
        target_type="deal",
        target_id=deal.id,
        actor_id=callback.from_user.id,
        actor_type="ADMIN",
        details={
            "new_status": deal.status,
            "deposit_address": deal.deposit_address,
            "recovered_from_timeout": was_closed,
        }
    )
    try:
        await db_session.commit()
    except IntegrityError:
        await db_session.rollback()
        await callback.answer(
            "⚠️ تعذّر استعادة الصفقة لوجود صفقة أخرى مفتوحة على نفس العرض حالياً.",
            show_alert=True
        )
        return

    if recovered_offer:
        try:
            await update_channel_offer_message(callback.bot, recovered_offer)
        except Exception as e:
            logger.warning(f"Failed to update channel offer message on deal #{deal.id} recovery: {e}")

    await push_deal_card(deal.id, callback.bot)

    if deal.status == "USDT_DEPOSITED" and deal.seller_id:
        seller_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 تحديد حساب الاستلام", callback_data=f"set_seller_info_{deal.id}")]
        ])
        try:
            await callback.bot.send_message(
                chat_id=deal.seller_id,
                text=(
                    f"🔒 **تحديث صفقة #{deal.id} — تم تأكيد إيداع الضمان**\n\n"
                    f"يرجى الضغط على الزر أدناه لتحديد حساب استلامك بالليرة السورية ليتمكن المشتري من التحويل:"
                ),
                reply_markup=seller_kb,
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Failed to prompt seller {deal.seller_id} for payment info on manual confirm deal #{deal.id}: {e}")

    try:
        await notify_usdt_deposit_confirmed(
            bot=callback.bot,
            deal=deal,
            db_session=db_session,
            verification_method=f"تأكيد يدوي من الأدمن ({callback.from_user.id})"
        )
    except Exception as e:
        logger.warning(f"Failed to send deposit channel notification on manual confirm for deal #{deal.id}: {e}")
        await report_handled_exception(
            callback.bot, e, context_label=f"notify_usdt_deposit_confirmed manual confirm (deal #{deal.id})", user_id=callback.from_user.id
        )

    await callback.answer("✅ تم تأكيد إيداع الـ USDT يدوياً وتحديث حالة الصفقة.", show_alert=True)
    admin_text = await format_admin_deal_review_text(deal, db_session)
    kb = await build_admin_deal_keyboard(deal, db_session)
    try:
        await callback.message.edit_text(admin_text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(admin_text, reply_markup=kb, parse_mode="Markdown")


@admin_router.callback_query(F.data.startswith("adm_ask_force_conf_"))
async def cq_admin_ask_force_confirm_syp(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    deal_id = int(callback.data.split("_")[-1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ نعم، تأكيد الاستلام إدارياً", callback_data=f"adm_do_force_conf_{deal_id}"),
            InlineKeyboardButton(text="↩️ تراجع", callback_data=f"adm_inspect_deal_{deal_id}")
        ]
    ])
    text = (
        f"⚡ **تأكيد إداري لاستلام الليرة — صفقة #{deal_id}**\n\n"
        "هل أنت متأكد من اعتماد استلام الليرة السورية نيابةً عن البائع المتأخر ونقل الصفقة إلى مرحلة `ADMIN_APPROVAL` لإطلاق الضمان؟"
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm_do_force_conf_"))
async def cq_admin_do_force_confirm_syp(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    deal_id = int(callback.data.split("_")[-1])
    res = await db_session.execute(select(Deal).where(Deal.id == deal_id).with_for_update())
    deal = res.scalar_one_or_none()
    if not deal:
        await callback.answer("الصفقة غير موجودة.", show_alert=True)
        return

    if deal.status != "SYP_PAYMENT_SENT":
        await callback.answer(f"لا يمكن تنفيذ هذا الإجراء، حالة الصفقة الحالية: {deal.status}", show_alert=True)
        return

    deal.status = "ADMIN_APPROVAL"
    deal.expires_at = None

    add_audit(
        session=db_session,
        action="ADMIN_FORCE_CONFIRM_SYP",
        target_type="deal",
        target_id=deal.id,
        actor_id=callback.from_user.id,
        actor_type="ADMIN",
        details={"seller_id": deal.seller_id, "buyer_id": deal.buyer_id}
    )
    await db_session.commit()

    await push_deal_card(deal.id, callback.bot)

    for uid in [deal.buyer_id, deal.seller_id]:
        try:
            await callback.bot.send_message(
                chat_id=uid,
                text=(
                    f"🛡️ **تحديث إداري — صفقة #{deal.id}**\n\n"
                    f"قامت إدارة {BOT_NAME} باعتماد تأكيد الحوالة ونقل الصفقة إلى مرحلة التدقيق النهائي لإطلاق الضمان (`ADMIN_APPROVAL`)."
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass

    await callback.answer("✅ تم تأكيد استلام الليرة إدارياً ونقل الصفقة للاعتماد.", show_alert=True)
    admin_text = await format_admin_deal_review_text(deal, db_session)
    kb = await build_admin_deal_keyboard(deal, db_session)
    try:
        await callback.message.edit_text(admin_text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(admin_text, reply_markup=kb, parse_mode="Markdown")

    # Also notify the dedicated Payouts Channel (ADMIN_PAYOUTS_CHANNEL_ID) if configured
    if settings.ADMIN_PAYOUTS_CHANNEL_ID:
        payout_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="☑️ اعتماد وإطلاق الضمان", callback_data=f"ask_rel_{deal.id}")],
            [InlineKeyboardButton(text="🔍 فحص تفصيلي للصفقة", callback_data=f"adm_inspect_deal_{deal.id}")]
        ])
        await dispatch_admin_notification(
            bot=callback.bot,
            category="payouts",
            text=admin_text,
            reply_markup=payout_kb,
            parse_mode="Markdown",
            exclude_admin_id=callback.from_user.id
        )


@admin_router.callback_query(F.data.startswith("adm_ask_force_dispute_"))
async def cq_admin_ask_force_dispute(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    deal_id = int(callback.data.split("_")[-1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⚠️ نعم، إغلاق بعدم الاستلام (DISPUTED)", callback_data=f"adm_do_force_dispute_{deal_id}"),
            InlineKeyboardButton(text="↩️ تراجع", callback_data=f"adm_inspect_deal_{deal_id}")
        ]
    ])
    text = (
        f"⚠️ **تأكيد إغلاق الصفقة #{deal_id} بحالة عدم استلام (`DISPUTED`)**\n\n"
        "سيتم إيقاف الصفقة وتثبيت حالتها كـ `DISPUTED` وإلغاء العرض المرتبط بها مع إشعار الطرفين.\n"
        "هل أنت متأكد من المتابعة؟"
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm_do_force_dispute_"))
async def cq_admin_do_force_dispute(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    deal_id = int(callback.data.split("_")[-1])
    res = await db_session.execute(select(Deal).where(Deal.id == deal_id).with_for_update())
    deal = res.scalar_one_or_none()
    if not deal:
        await callback.answer("الصفقة غير موجودة.", show_alert=True)
        return

    if deal.status in ["COMPLETED", "CANCELLED", "EXPIRED", "DISPUTED"]:
        await db_session.commit()
        await callback.answer(f"الصفقة مغلقة مسبقاً بالحالة: {deal.status}", show_alert=True)
        return

    if deal.status not in (
        "USDT_DEPOSITED",
        "AWAITING_SYP_PAYMENT",
        "SYP_PAYMENT_SENT",
        "SYP_PAYMENT_CONFIRMED",
        "ADMIN_APPROVAL",
    ):
        await db_session.commit()
        await callback.answer(
            "لا يمكن إغلاق الصفقة كنزاع قبل مرحلة إيداع الضمان؛ استخدم الإلغاء الإداري بدلاً من ذلك.",
            show_alert=True,
        )
        return

    prev_status = deal.status
    deal.status = "DISPUTED"
    deal.expires_at = None

    offer = None
    if deal.offer_id:
        offer_res = await db_session.execute(select(Offer).where(Offer.id == deal.offer_id).with_for_update())
        offer = offer_res.scalar_one_or_none()
        if offer:
            offer.status = "cancelled"
            offer.status_changed_at = datetime.now(timezone.utc)

    dispute_row = Dispute(
        deal_id=deal.id,
        opened_by=callback.from_user.id,
        reason="إغلاق إداري بحالة عدم استلام (DISPUTED)"
    )
    db_session.add(dispute_row)

    add_audit(
        session=db_session,
        action="ADMIN_CLOSE_DISPUTED",
        target_type="deal",
        target_id=deal.id,
        actor_id=callback.from_user.id,
        actor_type="ADMIN",
        details={"previous_status": prev_status}
    )
    await db_session.commit()

    if offer:
        await update_channel_offer_message(callback.bot, offer)

    await push_deal_card(deal.id, callback.bot)

    for uid in [deal.buyer_id, deal.seller_id]:
        try:
            await callback.bot.send_message(
                chat_id=uid,
                text=(
                    f"⚠️ **إشعار إداري — إغلاق صفقة #{deal.id}**\n\n"
                    f"تم إغلاق الصفقة رقم `#{deal.id}` إدارياً بحالة عدم استلام (`DISPUTED`).\n"
                    f"لأي استفسار يرجى التواصل مع الدعم الفني عبر `/support`."
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass

    await callback.answer("✅ تم إغلاق الصفقة بحالة DISPUTED.", show_alert=True)
    admin_text = await format_admin_deal_review_text(deal, db_session)
    kb = await build_admin_deal_keyboard(deal, db_session)
    try:
        await callback.message.edit_text(admin_text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(admin_text, reply_markup=kb, parse_mode="Markdown")


@admin_router.callback_query(F.data.startswith("adm_ask_force_cancel_"))
async def cq_admin_ask_force_cancel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    deal_id = int(callback.data.split("_")[-1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="❌ نعم، إلغاء الصفقة إدارياً", callback_data=f"adm_do_force_cancel_{deal_id}"),
            InlineKeyboardButton(text="↩️ تراجع", callback_data=f"adm_inspect_deal_{deal_id}")
        ]
    ])
    text = (
        f"❌ **تأكيد الإلغاء الإداري للصفقة #{deal_id}**\n\n"
        "سيتم إلغاء الصفقة (`CANCELLED`) وإعادة تفعيل العرض في السوق (إن كان صالحاً) وإشعار الطرفين.\n"
        "هل أنت متأكد من المتابعة؟"
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("adm_do_force_cancel_"))
async def cq_admin_do_force_cancel(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    deal_id = int(callback.data.split("_")[-1])
    res = await db_session.execute(select(Deal).where(Deal.id == deal_id).with_for_update())
    deal = res.scalar_one_or_none()
    if not deal:
        await callback.answer("الصفقة غير موجودة.", show_alert=True)
        return

    if deal.status in ["COMPLETED", "CANCELLED", "EXPIRED", "DISPUTED"]:
        await callback.answer(f"الصفقة مغلقة مسبقاً بالحالة: {deal.status}", show_alert=True)
        return

    if deal.status not in ("PENDING_OWNER_APPROVAL", "CREATED", "AWAITING_USDT_DEPOSIT"):
        await db_session.commit()
        await callback.answer(
            "⚠️ لا يمكن الإلغاء المباشر بعد إيداع الضمان؛ يجب تحويل الصفقة للنزاع أو التسوية اليدوية لإرجاع الرصيد للبائع.",
            show_alert=True,
        )
        return

    prev_status = deal.status
    deal.status = "CANCELLED"
    deal.expires_at = None

    offer = None
    if deal.offer_id:
        offer_res = await db_session.execute(select(Offer).where(Offer.id == deal.offer_id).with_for_update())
        offer = offer_res.scalar_one_or_none()
        if offer and offer.status == "reserved":
            now = datetime.now(timezone.utc)
            if offer.expires_at and offer.expires_at <= now:
                offer.status = "expired"
            else:
                offer.status = "active"

    add_audit(
        session=db_session,
        action="CANCEL_DEAL",
        target_type="deal",
        target_id=deal.id,
        actor_id=callback.from_user.id,
        actor_type="ADMIN",
        details={"previous_status": prev_status, "reason": "admin_force_cancel"}
    )
    await db_session.commit()

    if offer:
        await update_channel_offer_message(callback.bot, offer)

    await push_deal_card(deal.id, callback.bot)

    for uid in [deal.buyer_id, deal.seller_id]:
        try:
            await callback.bot.send_message(
                chat_id=uid,
                text=(
                    f"❌ **إشعار إداري — إلغاء صفقة #{deal.id}**\n\n"
                    f"قامت إدارة {BOT_NAME} بإلغاء الصفقة رقم `#{deal.id}` إدارياً."
                ),
                parse_mode="Markdown"
            )
        except Exception:
            pass

    await callback.answer("✅ تم إلغاء الصفقة إدارياً بنجاح.", show_alert=True)
    admin_text = await format_admin_deal_review_text(deal, db_session)
    kb = await build_admin_deal_keyboard(deal, db_session)
    try:
        await callback.message.edit_text(admin_text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(admin_text, reply_markup=kb, parse_mode="Markdown")


@admin_router.callback_query(F.data.startswith("adm_del_rating_"))
async def cq_admin_delete_rating(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك بالوصول.", show_alert=True)
        return

    parts = callback.data.split("_")
    if len(parts) != 4 or not parts[3].isdigit():
        await callback.answer("معرف التقييم غير صالح.", show_alert=True)
        return

    rating_id = int(parts[3])
    rating_res = await db_session.execute(
        select(DealRating).where(DealRating.id == rating_id).with_for_update()
    )
    rating = rating_res.scalar_one_or_none()
    if not rating:
        await callback.answer("التقييم غير موجود أو تم حذفه مسبقاً.", show_alert=True)
        return

    deal_id = rating.deal_id
    rater_id = rating.rater_id
    target_id = rating.target_id
    deleted_stars = rating.stars

    await db_session.delete(rating)
    await db_session.flush()

    new_rep, total_ratings = await recalculate_user_reputation(db_session, target_id)

    add_audit(
        session=db_session,
        action="DELETE_RATING",
        target_type="deal",
        target_id=deal_id,
        actor_id=callback.from_user.id,
        actor_type="ADMIN",
        details={
            "rating_id": rating_id,
            "rater_id": rater_id,
            "target_id": target_id,
            "deleted_stars": deleted_stars,
            "new_reputation_score": str(new_rep),
            "remaining_ratings": total_ratings
        }
    )
    await db_session.commit()

    deal_res = await db_session.execute(select(Deal).where(Deal.id == deal_id))
    deal = deal_res.scalar_one_or_none()
    if deal:
        admin_text = await format_admin_deal_review_text(deal, db_session)
        kb = await build_admin_deal_keyboard(deal, db_session)
        try:
            await callback.message.edit_text(admin_text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await callback.message.answer(admin_text, reply_markup=kb, parse_mode="Markdown")

    await callback.answer(
        f"✅ تم حذف التقييم وإعادة احتساب سمعة المستخدم ({new_rep}%).",
        show_alert=True
    )


# ================= 6. BROADCAST SYSTEM =================

@admin_router.message(Command("broadcast"))
async def cmd_admin_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminFSM.waiting_for_broadcast_text)
    text = (
        "📢 **إرسال رسالة بث جماعي**\n\n"
        f"أرسل الآن نص الرسالة المراد تعميمها على مستخدمي {BOT_NAME}:\n\n"
        "▫️ تدعم الرسالة التنسيق النصي.\n"
        "▫️ سيتم الإرسال لجميع الحسابات المسجلة تباعاً."
    )
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_main_menu")]
    ])
    await message.answer(text, reply_markup=cancel_kb, parse_mode="Markdown")


@admin_router.callback_query(F.data == "admin_broadcast")
async def cq_admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك بالوصول.", show_alert=True)
        return

    await state.set_state(AdminFSM.waiting_for_broadcast_text)
    text = (
        f"📢 **إرسال رسالة بث عام — {BOT_NAME}**\n\n"
        "أرسل نص الرسالة المطلوب تعميمها على مستخدمي البوت.\n\n"
        "▫️ تدعم الرسالة التنسيق النصي.\n"
        "▫️ يتم الإرسال لكافة الحسابات المسجلة تباعاً."
    )
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_main_menu")]
    ])
    await callback.message.edit_text(text, reply_markup=cancel_kb, parse_mode="Markdown")
    await callback.answer()


@admin_router.message(AdminFSM.waiting_for_broadcast_text)
async def process_broadcast_message(message: Message, state: FSMContext, db_session: AsyncSession, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    broadcast_text = message.text or message.caption
    if not broadcast_text:
        await message.answer("⚠️ يرجى إرسال نص للرسالة.")
        return

    await state.clear()

    users_res = await db_session.execute(select(User.telegram_id))
    all_user_ids = users_res.scalars().all()
    total_recipients = len(all_user_ids)

    status_msg = await message.answer(
        f"⏳ جاري إرسال البث لـ `{total_recipients}` مستخدم...\nيرجى الانتظار.",
        parse_mode="Markdown"
    )

    success_count = 0
    fail_count = 0

    formatted_broadcast = (
        f"📢 **تنبيه إداري — {BOT_NAME}**\n\n"
        f"{escape_markdown(broadcast_text)}"
    )

    for uid in all_user_ids:
        try:
            await bot.send_message(
                chat_id=uid,
                text=formatted_broadcast,
                parse_mode="Markdown"
            )
            success_count += 1
        except Exception:
            fail_count += 1
        await asyncio.sleep(0.04)

    summary_text = (
        "✅ **اكتملت عملية البث الجماعي**\n\n"
        f"▫️ **إجمالي المستهدفين:** `{total_recipients}`\n"
        f"▫️ **تم الإرسال بنجاح:** `{success_count}`\n"
        f"▫️ **فشل الإرسال (حظر أو حساب ملغى):** `{fail_count}`"
    )
    await status_msg.edit_text(summary_text, reply_markup=get_admin_back_kb(), parse_mode="Markdown")

    try:
        await record_audit(
            session=db_session,
            action="BROADCAST",
            target_type="system",
            target_id="broadcast",
            actor_id=message.from_user.id,
            actor_type="ADMIN",
            details={
                "total_recipients": total_recipients,
                "success_count": success_count,
                "fail_count": fail_count
            }
        )
    except Exception as e:
        logger.warning(f"Failed to record audit for broadcast: {e}")


# ================= 7. MAINTENANCE MODE TOGGLE =================

@admin_router.callback_query(F.data == "admin_toggle_maintenance")
async def cq_admin_toggle_maintenance(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك بالوصول.", show_alert=True)
        return

    result = await db_session.execute(
        select(SystemSetting).where(SystemSetting.key == "maintenance_mode").with_for_update()
    )
    setting = result.scalar_one_or_none()

    current_val = False
    if setting:
        if isinstance(setting.value, dict):
            current_val = bool(setting.value.get("enabled", False))
        elif isinstance(setting.value, bool):
            current_val = setting.value

    new_val = not current_val

    if not setting:
        setting = SystemSetting(
            key="maintenance_mode",
            value={"enabled": new_val}
        )
        db_session.add(setting)
    else:
        setting.value = {"enabled": new_val}

    add_audit(
        session=db_session,
        action="MAINTENANCE_MODE_ON" if new_val else "MAINTENANCE_MODE_OFF",
        target_type="system_setting",
        target_id="maintenance_mode",
        actor_id=callback.from_user.id,
        actor_type="ADMIN",
        details={"enabled": new_val, "method": "button"}
    )

    await db_session.commit()
    set_cached_maintenance(new_val)

    status_str = "🟢 تم تفعيل وضع الصيانة! (الوصول محصور بالأدمن فقط)" if new_val else "🔴 تم تعطيل وضع الصيانة! (الوصول متاح للجميع)"
    await callback.answer(status_str, show_alert=True)

    text, kb = await _build_admin_dashboard_view(db_session)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")


# ================= 8. DATABASE BACKUP (/backup & button) =================

@admin_router.message(Command("backup"))
async def cmd_admin_backup(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    wait_msg = await message.answer(
        "⏳ **جاري إنشاء وحزم نسخة احتياطية كاملة لقاعدة البيانات...**\nيرجى الانتظار بضع ثوانٍ...",
        parse_mode="Markdown"
    )

    try:
        await perform_and_send_backup(bot, recipient_id=message.from_user.id, is_scheduled=False)
        try:
            await wait_msg.delete()
        except Exception:
            pass
    except Exception as e:
        await wait_msg.edit_text(f"❌ **حدث خطأ أثناء أخذ النسخة الاحتياطية:**\n`{e}`", parse_mode="Markdown")
        await report_handled_exception(
            bot, e, context_label="cmd_admin_backup", user_id=message.from_user.id
        )


@admin_router.callback_query(F.data == "adm_trigger_backup")
async def cq_admin_trigger_backup(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    await callback.answer("⏳ جاري تجهيز وإرسال النسخة الاحتياطية...")
    try:
        await perform_and_send_backup(callback.bot, recipient_id=callback.from_user.id, is_scheduled=False)
    except Exception as e:
        await callback.message.answer(f"❌ **حدث خطأ أثناء أخذ النسخة الاحتياطية:**\n`{e}`", parse_mode="Markdown")
        await report_handled_exception(
            callback.bot, e, context_label="cq_admin_trigger_backup", user_id=callback.from_user.id
        )


# ================= 9. AUDIT TRAIL & SYSTEM LOGS (/audit & button) =================

ACTION_TITLES = {
    "CREATE_DEAL": "🤝 إنشاء صفقة جديدة",
    "RELEASE_ESCROW": "☑️ إطلاق ضمان مالي",
    "CANCEL_DEAL": "❌ إلغاء صفقة",
    "ADMIN_MANUAL_CONFIRM_DEPOSIT": "✅ تأكيد إيداع ضمان يدوياً",
    "ADMIN_FORCE_CONFIRM_SYP": "⚡ تأكيد استلام ليرة إدارياً",
    "ADMIN_CLOSE_DISPUTED": "⚠️ إغلاق صفقة بعدم استلام",
    "ADMIN_DELETE_OFFER": "🗑️ حذف عرض إدارياً",
    "ADMIN_APPROVE_OFFER_NOTE": "✅ موافقة ونشر عرض بملاحظة مخصصة",
    "ADMIN_REJECT_OFFER_NOTE": "❌ رفض عرض بملاحظة مخصصة",
    "EXPIRE_PENDING_OFFER_NOTE": "⌛ انتهاء مهلة مراجعة ملاحظة عرض",
    "BAN_USER": "🚫 حظر مستخدم",
    "UNBAN_USER": "✅ فك حظر مستخدم",
    "MAINTENANCE_MODE_ON": "🟢 تفعيل الصيانة",
    "MAINTENANCE_MODE_OFF": "🔴 تعطيل الصيانة",
    "DISPUTE_RESOLVE": "⚖️ تسوية نزاع",
    "RATE_DEAL": "⭐ تقييم صفقة",
    "DELETE_RATING": "🗑️ حذف تقييم خاطئ",
    "BROADCAST": "📢 بث إداري جماعي",
}


async def _build_audit_view(db_session: AsyncSession, f_type: str | None = None, f_id: str | None = None) -> tuple[str, InlineKeyboardMarkup]:
    query = select(AuditLog).order_by(AuditLog.id.desc())
    filter_label = "آخر العمليات الإدارية والمالية المسجلة"
    if f_type and f_id:
        query = query.where(AuditLog.target_type == f_type, AuditLog.target_id == f_id)
        filter_label = f"سجل العمليات الخاص بـ `{f_type}:{f_id}`"

    query = query.limit(10)
    result = await db_session.execute(query)
    logs = result.scalars().all()

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📁 تحميل ملف السجلات", callback_data="adm_dl_system_log"),
            InlineKeyboardButton(text="🔄 تحديث السجل", callback_data="adm_refresh_audit")
        ],
        [InlineKeyboardButton(text="🔙 العودة للوحة الإدارة", callback_data="admin_main_menu")]
    ])

    if not logs:
        return (
            "📋 **سجل التدقيق والعمليات**\n\n"
            "لا توجد سجلات مطابقة لطلبك حتى الآن.",
            kb
        )

    lines = [
        "📋 **سجل التدقيق والعمليات**\n\n",
        f"▫️ **الفلترة:** {filter_label}\n\n",
    ]

    for log in logs:
        time_str = format_local_datetime(log.created_at) if log.created_at else "—"
        act_title = escape_markdown(ACTION_TITLES.get(log.action, log.action))
        actor_type_safe = escape_markdown(log.actor_type)
        actor_tag = f"`{log.actor_id}`" if log.actor_id else "SYSTEM"
        lines.append(
            f"🔹 **#{log.id} — {act_title}**\n"
            f"▫️ **المنفّذ:** {actor_type_safe} ({actor_tag})\n"
            f"▫️ **الهدف:** `{log.target_type}:{log.target_id}`\n"
            f"▫️ **التوقيت:** `{time_str}`\n\n"
        )

    lines.append("💡 للتدقيق في صفقة أو مستخدم محدد:\n`/audit deal [رقم]` أو `/audit user [معرف]`")
    return "".join(lines), kb


@admin_router.message(Command("audit"))
async def cmd_admin_audit(message: Message, db_session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    args = (message.text or "").split()
    f_type = args[1].lower() if len(args) >= 3 else None
    f_id = args[2] if len(args) >= 3 else None

    text, kb = await _build_audit_view(db_session, f_type, f_id)
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@admin_router.callback_query(F.data.in_(["adm_open_audit", "adm_refresh_audit"]))
async def cq_admin_refresh_audit(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    text, kb = await _build_audit_view(db_session)
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        pass
    await callback.answer()


@admin_router.callback_query(F.data == "adm_dl_system_log")
async def cq_admin_download_log(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    import os
    from aiogram.types import FSInputFile

    if not os.path.exists(LOG_FILE):
        await callback.answer("ملف السجلات لم يُنشأ بعد.", show_alert=True)
        return

    try:
        doc = FSInputFile(path=LOG_FILE, filename="escrow_system.log")
        await callback.message.answer_document(
            document=doc,
            caption="📁 **ملف السجلات التقنية الدوارة للنظام (Rotating System Log)**",
            parse_mode="Markdown"
        )
        await callback.answer()
    except Exception as e:
        await callback.answer(f"خطأ: {e}", show_alert=True)


# ================= 10. CUSTOM OFFER NOTE APPROVAL & REJECTION =================

@admin_router.callback_query(F.data.startswith("adm_note_app_"))
async def cq_admin_approve_offer_note(callback: CallbackQuery, db_session: AsyncSession, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    offer_id_str = callback.data.replace("adm_note_app_", "")
    if not offer_id_str.isdigit():
        await callback.answer("معرف العرض غير صالح.", show_alert=True)
        return
    offer_id = int(offer_id_str)

    result = await db_session.execute(
        select(Offer).where(Offer.id == offer_id).with_for_update()
    )
    offer = result.scalar_one_or_none()
    if not offer:
        await callback.answer("لم يتم العثور على العرض.", show_alert=True)
        return

    if offer.status != "pending_approval":
        await db_session.commit()
        await callback.answer(f"تمت معالجة هذا العرض مسبقاً (الحالة الحالية: {offer.status}).", show_alert=True)
        return

    now = datetime.now(timezone.utc)
    dur_hours = float(offer.duration_hours or 4.0)
    offer.status = "active"
    offer.status_changed_at = now
    offer.expires_at = now + timedelta(hours=dur_hours)

    add_audit(
        session=db_session,
        action="ADMIN_APPROVE_OFFER_NOTE",
        target_type="offer",
        target_id=str(offer.id),
        actor_id=callback.from_user.id,
        actor_type="ADMIN",
        details={"note": offer.note, "duration_hours": dur_hours}
    )
    await db_session.commit()
    await db_session.refresh(offer)

    duration_str = format_duration_hours_minutes(dur_hours)

    if settings.CHANNEL_ID and settings.BOT_USERNAME:
        trader_res = await db_session.execute(select(User).where(User.telegram_id == offer.user_id))
        trader_user = trader_res.scalar_one_or_none()
        channel_text, kb_chan = build_channel_offer_message(
            offer,
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
            offer.channel_message_id = msg.message_id
            await db_session.commit()
        except Exception as e:
            logger.warning(f"Failed to broadcast approved offer #{offer.id} to channel: {e}")

    if offer.user_id:
        try:
            await bot.send_message(
                chat_id=offer.user_id,
                text=(
                    f"✅ **تمت الموافقة على ملاحظتك ونشر العرض #{offer.id}!**\n"
                    "〰️〰️〰️〰️〰️〰️〰️\n"
                    f"📝 **الملاحظة المعتمدة:** `{escape_markdown(str(offer.note or ''))}`\n"
                    f"⏳ **مدة الصلاحية:** `{duration_str}`\n\n"
                    "أصبح عرضك نشطاً الآن في السوق والقناة الرسمية."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Failed to notify offer owner {offer.user_id} of note approval: {e}")

    admin_tag = safe_username(callback.from_user.username)
    updated_card = (
        f"✅ **تم اعتماد الملاحظة ونشر العرض #{offer.id}**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👤 **صاحب العرض:** `{offer.user_id}`\n"
        f"📝 **الملاحظة:** `{escape_markdown(str(offer.note or ''))}`\n"
        f"🛡️ **بواسطة الأدمن:** `{callback.from_user.id}` ({admin_tag})"
    )
    try:
        await callback.message.edit_text(updated_card, parse_mode="Markdown")
    except Exception:
        pass
    await callback.answer("تمت الموافقة ونشر العرض بنجاح.")


@admin_router.callback_query(F.data.startswith("adm_note_rej_"))
async def cq_admin_reject_offer_note_prompt(callback: CallbackQuery, db_session: AsyncSession, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    if callback.data == "adm_note_rej_cancel":
        await state.clear()
        try:
            await callback.message.edit_text("↩️ تم التراجع عن عملية رفض العرض.")
        except Exception:
            pass
        await callback.answer("تم الإلغاء.")
        return

    offer_id_str = callback.data.replace("adm_note_rej_", "")
    if not offer_id_str.isdigit():
        await callback.answer("معرف العرض غير صالح.", show_alert=True)
        return
    offer_id = int(offer_id_str)

    result = await db_session.execute(select(Offer).where(Offer.id == offer_id))
    offer = result.scalar_one_or_none()
    if not offer:
        await callback.answer("لم يتم العثور على العرض.", show_alert=True)
        return

    if offer.status != "pending_approval":
        await callback.answer(f"تمت معالجة هذا العرض مسبقاً (الحالة الحالية: {offer.status}).", show_alert=True)
        return

    await state.update_data(
        reject_offer_id=offer_id,
        reject_card_chat_id=callback.message.chat.id,
        reject_card_msg_id=callback.message.message_id,
    )
    await state.set_state(AdminFSM.waiting_for_note_reject_reason)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ تراجع عن الرفض", callback_data="adm_note_rej_cancel")]
    ])
    await callback.message.answer(
        f"✍️ **رفض العرض #{offer_id} مع ذكر السبب**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"📝 **الملاحظة المرفوضة:** `{escape_markdown(str(offer.note or ''))}`\n\n"
        "أرسل الآن **سبب الرفض** ليتم إرساله إلى صاحب العرض وإلغاء العرض:",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await callback.answer()


@admin_router.message(AdminFSM.waiting_for_note_reject_reason)
async def process_admin_reject_offer_note_reason(message: Message, db_session: AsyncSession, state: FSMContext, bot: Bot):
    if not is_admin(message.from_user.id):
        await state.clear()
        return

    reason_text = " ".join((message.text or "").strip().split())
    if not reason_text:
        await message.answer("⚠️ يرجى إرسال سبب واضح للرفض:")
        return

    data = await state.get_data()
    offer_id = data.get("reject_offer_id")
    card_chat_id = data.get("reject_card_chat_id")
    card_msg_id = data.get("reject_card_msg_id")
    await state.clear()

    if not offer_id:
        await message.answer("⚠️ انتهت جلسة الرفض، يرجى الضغط على زر الرفض مجدداً.")
        return

    result = await db_session.execute(
        select(Offer).where(Offer.id == int(offer_id)).with_for_update()
    )
    offer = result.scalar_one_or_none()
    if not offer:
        await message.answer("⚠️ لم يتم العثور على العرض.")
        return

    if offer.status != "pending_approval":
        await db_session.commit()
        await message.answer(f"⚠️ تمت معالجة هذا العرض مسبقاً (الحالة الحالية: `{offer.status}`).", parse_mode="Markdown")
        return

    offer.status = "cancelled"
    offer.status_changed_at = datetime.now(timezone.utc)

    add_audit(
        session=db_session,
        action="ADMIN_REJECT_OFFER_NOTE",
        target_type="offer",
        target_id=str(offer.id),
        actor_id=message.from_user.id,
        actor_type="ADMIN",
        details={"note": offer.note, "reason": reason_text}
    )
    await db_session.commit()

    safe_reason = escape_markdown(reason_text)
    safe_note = escape_markdown(str(offer.note or ""))

    if offer.user_id:
        try:
            await bot.send_message(
                chat_id=offer.user_id,
                text=(
                    f"❌ **تم رفض نشر العرض #{offer.id}**\n"
                    "〰️〰️〰️〰️〰️〰️〰️\n"
                    f"📝 **الملاحظة المدخلة:** `{safe_note}`\n"
                    f"📌 **سبب الرفض من الإدارة:**\n{safe_reason}\n"
                    "〰️〰️〰️〰️〰️〰️〰️\n"
                    "يمكنك إنشاء عرض جديد بملاحظة مطابقة لشروط المنصة أو استخدام الاقتراحات الجاهزة."
                ),
                parse_mode="Markdown"
            )
        except Exception as e:
            logger.warning(f"Failed to notify offer owner {offer.user_id} of note rejection: {e}")

    admin_tag = safe_username(message.from_user.username)
    updated_card = (
        f"❌ **تم رفض العرض #{offer.id} وإلغاؤه**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👤 **صاحب العرض:** `{offer.user_id}`\n"
        f"📝 **الملاحظة المرفوضة:** `{safe_note}`\n"
        f"📌 **سبب الرفض:** {safe_reason}\n"
        f"🛡️ **بواسطة الأدمن:** `{message.from_user.id}` ({admin_tag})"
    )
    if card_chat_id and card_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=card_chat_id,
                message_id=card_msg_id,
                text=updated_card,
                parse_mode="Markdown"
            )
        except Exception:
            pass

    await message.answer(
        f"✅ تم رفض العرض `#{offer.id}` وإرسال السبب إلى المستخدم بنجاح.",
        parse_mode="Markdown"
    )


# ================= 11. KYC IDENTITY VERIFICATION REVIEW =================

@admin_router.callback_query(F.data.startswith("adm_kyc_view_") | F.data.startswith("adm_user_kyc_"))
async def cq_admin_view_kyc_request(callback: CallbackQuery, db_session: AsyncSession, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ غير مصرح.", show_alert=True)
        return

    if callback.data.startswith("adm_kyc_view_"):
        try:
            req_id = int(callback.data.replace("adm_kyc_view_", ""))
        except ValueError:
            await callback.answer("⚠️ رقم الطلب غير صالح.", show_alert=True)
            return
        res = await db_session.execute(select(KycRequest).where(KycRequest.id == req_id))
        kyc_req = res.scalar_one_or_none()
    else:
        try:
            uid = int(callback.data.replace("adm_user_kyc_", ""))
        except ValueError:
            await callback.answer("⚠️ معرف المستخدم غير صالح.", show_alert=True)
            return
        res = await db_session.execute(
            select(KycRequest)
            .where(KycRequest.user_id == uid)
            .order_by(KycRequest.id.desc())
            .limit(1)
        )
        kyc_req = res.scalar_one_or_none()

    if not kyc_req:
        await callback.answer("⚠️ لم يتم العثور على طلب توثيق هوية لهذا المستخدم.", show_alert=True)
        return

    await callback.answer("🪪 جارٍ تحميل صور وبيانات التوثيق...")

    user_res = await db_session.execute(select(User).where(User.telegram_id == kyc_req.user_id))
    user = user_res.scalar_one_or_none()
    uname = safe_username(user.username if user else None, kyc_req.user_id)
    nick = escape_markdown(user.nickname) if user and getattr(user, "nickname", None) else "غير محدد"

    try:
        media = [
            InputMediaPhoto(
                media=kyc_req.id_front_file_id,
                caption=f"1️⃣ الوثيقة من الأمام — طلب #{kyc_req.id} (المستخدم: {kyc_req.user_id})",
            ),
            InputMediaPhoto(
                media=kyc_req.id_back_file_id,
                caption=f"2️⃣ الوثيقة من الخلف — طلب #{kyc_req.id}",
            ),
            InputMediaPhoto(
                media=kyc_req.selfie_file_id,
                caption=f"3️⃣ سيلفي التحقق مع الوثيقة والورقة — طلب #{kyc_req.id}",
            ),
        ]
        await bot.send_media_group(chat_id=callback.message.chat.id, media=media)
    except Exception as e:
        logger.warning(f"Failed to send KYC media group for request #{kyc_req.id}: {e}")

    status_ar = {
        "pending": "🟡 قيد المراجعة ⏳",
        "approved": "🟢 مقبول ومعتمد ✅",
        "rejected": "🔴 مرفوض ❌",
    }.get(kyc_req.status, kyc_req.status)

    safe_fn = escape_markdown(str(kyc_req.full_name or ""))
    safe_nid = escape_markdown(str(kyc_req.national_id or ""))
    safe_ph = escape_markdown(str(kyc_req.phone_number or ""))
    created_str = format_local_datetime(kyc_req.created_at) if kyc_req.created_at else "غير محدد"
    reviewed_str = format_local_datetime(kyc_req.reviewed_at) if kyc_req.reviewed_at else "—"

    rej_block = ""
    if kyc_req.status == "rejected" and kyc_req.rejection_reason:
        rej_block = f"📌 **سبب الرفض:** `{escape_markdown(kyc_req.rejection_reason)}`\n"

    card_text = (
        f"🪪 **ملف توثيق الهوية (KYC) — طلب #{kyc_req.id}**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👤 **المستخدم:** `{kyc_req.user_id}` ({uname})\n"
        f"🏷️ **اللقب المسجل:** `{nick}`\n"
        f"📊 **حالة الطلب:** {status_ar}\n"
        f"📅 **تاريخ التقديم:** `{created_str}`\n"
        f"🛡️ **تاريخ المراجعة:** `{reviewed_str}`\n"
        f"{rej_block}"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"📝 **الاسم الثلاثي:** `{safe_fn}`\n"
        f"🔢 **الرقم الوطني:** `{safe_nid}`\n"
        f"📱 **رقم الموبايل:** `{safe_ph}`"
    )

    kb_rows = []
    if kyc_req.status == "pending":
        kb_rows.append([
            InlineKeyboardButton(text="✅ قبول وتوثيق الحساب", callback_data=f"adm_kyc_app_{kyc_req.id}"),
            InlineKeyboardButton(text="❌ رفض مع ذكر السبب", callback_data=f"adm_kyc_rej_{kyc_req.id}"),
        ])
    elif user and getattr(user, "kyc_status", "unverified") == "verified":
        kb_rows.append([
            InlineKeyboardButton(text="⚠️ إلغاء توثيق الهوية للمستخدم", callback_data=f"adm_kyc_revoke_{kyc_req.user_id}"),
        ])

    kb_rows.append([
        InlineKeyboardButton(text="👤 فتح ملف المستخدم", callback_data=f"adm_back_user_{kyc_req.user_id}"),
        InlineKeyboardButton(text="🔙 لوحة الإدارة", callback_data="admin_main_menu"),
    ])

    await callback.message.answer(
        card_text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
        parse_mode="Markdown",
    )


@admin_router.callback_query(F.data.startswith("adm_kyc_app_"))
async def cq_admin_approve_kyc(callback: CallbackQuery, db_session: AsyncSession, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ غير مصرح.", show_alert=True)
        return

    try:
        req_id = int(callback.data.replace("adm_kyc_app_", ""))
    except ValueError:
        await callback.answer("⚠️ رقم الطلب غير صالح.", show_alert=True)
        return

    res = await db_session.execute(
        select(KycRequest).where(KycRequest.id == req_id).with_for_update()
    )
    kyc_req = res.scalar_one_or_none()
    if not kyc_req:
        await callback.answer("⚠️ الطلب غير موجود.", show_alert=True)
        return

    if kyc_req.status != "pending":
        await callback.answer(f"ℹ️ تمت معالجة هذا الطلب مسبقاً (الحالة: {kyc_req.status}).", show_alert=True)
        return

    user_res = await db_session.execute(
        select(User).where(User.telegram_id == kyc_req.user_id).with_for_update()
    )
    user = user_res.scalar_one_or_none()
    if not user:
        await callback.answer("⚠️ المستخدم غير موجود في قاعدة البيانات.", show_alert=True)
        return

    prev_user_kyc_status = getattr(user, "kyc_status", "unverified") or "unverified"
    if prev_user_kyc_status == "verified":
        await db_session.commit()
        await callback.answer("ℹ️ حساب هذا المستخدم موثق بالفعل.", show_alert=True)
        return

    now_utc = datetime.now(timezone.utc)
    kyc_req.status = "approved"
    kyc_req.reviewed_by = callback.from_user.id
    kyc_req.reviewed_at = now_utc
    user.kyc_status = "verified"

    add_audit(
        session=db_session,
        action="ADMIN_APPROVE_KYC",
        target_type="kyc_request",
        target_id=str(kyc_req.id),
        actor_id=callback.from_user.id,
        actor_type="ADMIN",
        details={
            "kyc_request_id": kyc_req.id,
            "user_id": kyc_req.user_id,
            "previous_status": prev_user_kyc_status,
        },
    )
    await db_session.commit()

    # Refresh any active channel offer posts for this trader so the verified badge appears immediately
    try:
        open_offers_res = await db_session.execute(
            select(Offer).where(
                Offer.user_id == kyc_req.user_id,
                Offer.channel_message_id.is_not(None),
                Offer.status.in_(["active", "reserved", "negotiating"]),
            )
        )
        for off in open_offers_res.scalars().all():
            await update_channel_offer_message(bot, off, trader=user)
    except Exception as e:
        logger.warning(f"Failed to refresh channel offers after KYC approval for user {kyc_req.user_id}: {e}")

    try:
        await bot.send_message(
            chat_id=kyc_req.user_id,
            text=(
                f"🟢 **تم اعتماد توثيق هويتك رسمياً لدى {BOT_NAME}** ✅\n"
                "〰️〰️〰️〰️〰️〰️〰️\n"
                f"🆔 **رقم الطلب:** `#{kyc_req.id}`\n"
                "✨ أصبح حسابك الآن يحمل شارة **موثق ✅** في ملفك الشخصي."
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👤 عرض ملفي الشخصي", callback_data="menu_user_profile")]
            ]),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"Failed to notify user {kyc_req.user_id} of KYC approval: {e}")

    admin_tag = safe_username(callback.from_user.username, callback.from_user.id)
    safe_fn = escape_markdown(str(kyc_req.full_name or ""))
    safe_nid = escape_markdown(str(kyc_req.national_id or ""))
    safe_ph = escape_markdown(str(kyc_req.phone_number or ""))
    updated_text = (
        f"✅ **تم اعتماد توثيق الهوية — طلب #{kyc_req.id}**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👤 **المستخدم:** `{kyc_req.user_id}`\n"
        f"📝 **الاسم الثلاثي:** `{safe_fn}`\n"
        f"🔢 **الرقم الوطني:** `{safe_nid}`\n"
        f"📱 **رقم الموبايل:** `{safe_ph}`\n"
        f"🛡️ **بواسطة الأدمن:** `{callback.from_user.id}` ({admin_tag})"
    )
    try:
        await callback.message.edit_text(updated_text, parse_mode="Markdown")
    except Exception:
        pass

    await callback.answer("✅ تم اعتماد توثيق الهوية وإشعار المستخدم بنجاح.", show_alert=True)


@admin_router.callback_query(F.data.startswith("adm_kyc_rej_"))
async def cq_admin_reject_kyc_prompt(callback: CallbackQuery, db_session: AsyncSession, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ غير مصرح.", show_alert=True)
        return

    if callback.data == "adm_kyc_rej_cancel":
        await state.clear()
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.answer("تم إلغاء عملية الرفض.")
        return

    try:
        req_id = int(callback.data.replace("adm_kyc_rej_", ""))
    except ValueError:
        await callback.answer("⚠️ رقم الطلب غير صالح.", show_alert=True)
        return

    res = await db_session.execute(select(KycRequest).where(KycRequest.id == req_id))
    kyc_req = res.scalar_one_or_none()
    if not kyc_req:
        await callback.answer("⚠️ الطلب غير موجود.", show_alert=True)
        return

    if kyc_req.status != "pending":
        await callback.answer(f"ℹ️ تمت معالجة هذا الطلب مسبقاً (الحالة: {kyc_req.status}).", show_alert=True)
        return

    await state.set_state(AdminFSM.waiting_for_kyc_reject_reason)
    await state.update_data(
        reject_kyc_req_id=req_id,
        reject_kyc_card_chat_id=callback.message.chat.id,
        reject_kyc_card_msg_id=callback.message.message_id,
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 تراجع عن الرفض", callback_data="adm_kyc_rej_cancel")]
    ])
    await callback.message.answer(
        f"✍️ **رفض طلب توثيق الهوية #{req_id}**\n\n"
        "أرسل الآن سبب الرفض ليتم إرساله إلى المستخدم لتصحيح بياناته أو إعادة التصوير:",
        reply_markup=kb,
        parse_mode="Markdown",
    )
    await callback.answer()


@admin_router.message(AdminFSM.waiting_for_kyc_reject_reason)
async def process_admin_reject_kyc_reason(message: Message, state: FSMContext, db_session: AsyncSession, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    reason_text = (message.text or "").strip()
    if not reason_text:
        await message.answer("⚠️ يرجى كتابة سبب الرفض كنص واضح.")
        return

    data = await state.get_data()
    req_id = data.get("reject_kyc_req_id")
    card_chat_id = data.get("reject_kyc_card_chat_id")
    card_msg_id = data.get("reject_kyc_card_msg_id")
    await state.clear()

    if not req_id:
        await message.answer("⚠️ انتهت جلسة الرفض، يرجى المحاولة مجدداً.")
        return

    res = await db_session.execute(
        select(KycRequest).where(KycRequest.id == int(req_id)).with_for_update()
    )
    kyc_req = res.scalar_one_or_none()
    if not kyc_req:
        await message.answer("⚠️ طلب التوثيق غير موجود.")
        return

    if kyc_req.status != "pending":
        await message.answer(f"ℹ️ تمت معالجة الطلب `#{kyc_req.id}` مسبقاً (الحالة: `{kyc_req.status}`).", parse_mode="Markdown")
        return

    user_res = await db_session.execute(
        select(User).where(User.telegram_id == kyc_req.user_id).with_for_update()
    )
    user = user_res.scalar_one_or_none()

    now_utc = datetime.now(timezone.utc)
    kyc_req.status = "rejected"
    kyc_req.rejection_reason = reason_text[:500]
    kyc_req.reviewed_by = message.from_user.id
    kyc_req.reviewed_at = now_utc
    if user:
        user.kyc_status = "rejected"

    add_audit(
        session=db_session,
        action="ADMIN_REJECT_KYC",
        target_type="kyc_request",
        target_id=str(kyc_req.id),
        actor_id=message.from_user.id,
        actor_type="ADMIN",
        details={
            "user_id": kyc_req.user_id,
            "reason": reason_text[:500],
        },
    )
    await db_session.commit()

    safe_reason = escape_markdown(reason_text[:500])

    try:
        await bot.send_message(
            chat_id=kyc_req.user_id,
            text=(
                f"❌ **تعذر اعتماد طلب توثيق الهوية #{kyc_req.id}**\n"
                "〰️〰️〰️〰️〰️〰️〰️\n"
                f"📌 **سبب الرفض من الإدارة:**\n{safe_reason}\n"
                "〰️〰️〰️〰️〰️〰️〰️\n"
                "يمكنك إعادة تقديم طلب التوثيق مع مراعاة الملاحظة أعلاه."
            ),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 إعادة تقديم طلب التوثيق", callback_data="prof_kyc")],
                [InlineKeyboardButton(text="👤 ملفي الشخصي", callback_data="menu_user_profile")],
            ]),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"Failed to notify user {kyc_req.user_id} of KYC rejection: {e}")

    admin_tag = safe_username(message.from_user.username, message.from_user.id)
    safe_fn = escape_markdown(str(kyc_req.full_name or ""))
    updated_card = (
        f"❌ **تم رفض طلب توثيق الهوية #{kyc_req.id}**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👤 **المستخدم:** `{kyc_req.user_id}`\n"
        f"📝 **الاسم الثلاثي:** `{safe_fn}`\n"
        f"📌 **سبب الرفض:** {safe_reason}\n"
        f"🛡️ **بواسطة الأدمن:** `{message.from_user.id}` ({admin_tag})"
    )
    if card_chat_id and card_msg_id:
        try:
            await bot.edit_message_text(
                chat_id=card_chat_id,
                message_id=card_msg_id,
                text=updated_card,
                parse_mode="Markdown",
            )
        except Exception:
            pass

    await message.answer(
        f"✅ تم رفض طلب التوثيق `#{kyc_req.id}` وإرسال السبب إلى المستخدم بنجاح.",
        parse_mode="Markdown",
    )


@admin_router.callback_query(F.data.startswith("adm_kyc_revoke_"))
async def cq_admin_revoke_kyc(callback: CallbackQuery, db_session: AsyncSession, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ غير مصرح.", show_alert=True)
        return

    try:
        uid = int(callback.data.replace("adm_kyc_revoke_", ""))
    except ValueError:
        await callback.answer("⚠️ معرف المستخدم غير صالح.", show_alert=True)
        return

    user_res = await db_session.execute(
        select(User).where(User.telegram_id == uid).with_for_update()
    )
    user = user_res.scalar_one_or_none()
    if not user:
        await callback.answer("⚠️ المستخدم غير موجود.", show_alert=True)
        return

    prev_status = getattr(user, "kyc_status", "unverified") or "unverified"
    if prev_status != "verified":
        await db_session.commit()
        await callback.answer(
            f"ℹ️ لا يمكن إلغاء التوثيق: حالة المستخدم الحالية هي ({prev_status}) وليست موثقة.",
            show_alert=True,
        )
        return

    user.kyc_status = "unverified"
    add_audit(
        session=db_session,
        action="ADMIN_REVOKE_KYC",
        target_type="user",
        target_id=str(uid),
        actor_id=callback.from_user.id,
        actor_type="ADMIN",
        details={"previous_status": prev_status, "new_status": "unverified"},
    )
    await db_session.commit()

    # Refresh any active channel offer posts for this trader so the verified badge is updated immediately
    try:
        open_offers_res = await db_session.execute(
            select(Offer).where(
                Offer.user_id == uid,
                Offer.channel_message_id.is_not(None),
                Offer.status.in_(["active", "reserved", "negotiating"]),
            )
        )
        for off in open_offers_res.scalars().all():
            await update_channel_offer_message(bot, off, trader=user)
    except Exception as e:
        logger.warning(f"Failed to refresh channel offers after KYC revocation for user {uid}: {e}")

    try:
        await bot.send_message(
            chat_id=uid,
            text=(
                "⚠️ **إشعار من الإدارة — تحديث حالة توثيق الهوية**\n"
                "〰️〰️〰️〰️〰️〰️〰️\n"
                "تم إلغاء حالة توثيق الهوية لحسابك من قبل الإدارة. يمكنك إعادة تقديم طلب توثيق جديد من ملفك الشخصي."
            ),
            parse_mode="Markdown",
        )
    except Exception:
        pass

    await callback.answer("✅ تم إلغاء توثيق الهوية للمستخدم.", show_alert=True)
    await send_user_card(callback, user, db_session, is_edit=True)


