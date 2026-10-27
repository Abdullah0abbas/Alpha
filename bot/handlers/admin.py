from decimal import Decimal
from datetime import datetime, timezone
import asyncio
import math
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func

from db.models import User, Deal, Offer, SystemSetting, Dispute
from bot.states.flow_states import AdminFSM
from bot.middlewares.maintenance import get_cached_maintenance, set_cached_maintenance
from bot.utils.formatters import format_usdt, format_syp, safe_username, escape_markdown, format_local_datetime
from bot.utils.deal_views import format_admin_deal_review_text
from bot.services.backup_service import perform_and_send_backup
import logging
from bot.services.audit_service import record_audit, add_audit, LOG_FILE
from db.models import AuditLog
from config.settings import settings
from bot.utils.branding import BOT_NAME

logger = logging.getLogger(__name__)

admin_router = Router()

def is_admin(user_id: int) -> bool:
    return user_id in settings.ADMIN_IDS

def get_admin_main_kb(maintenance_on: bool) -> InlineKeyboardMarkup:
    m_icon = "🟢 مفعّل" if maintenance_on else "🔴 معطّل"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 إحصائيات النظام", callback_data="admin_stats"),
            InlineKeyboardButton(text="🔍 استعلام عن مستخدم", callback_data="admin_search_user")
        ],
        [
            InlineKeyboardButton(text="📢 رسالة بث جماعي", callback_data="admin_broadcast"),
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

# ================= 1. ADMIN MENU ENTRYPOINT =================

@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, db_session: AsyncSession, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    
    # Check current maintenance setting
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

    m_status_ar = "🟢 مفعل (الوصول محصور بالأدمن)" if is_m else "🔴 معطل (الوصول متاح للجميع)"

    text = (
        f"👑 **لوحة تحكم {BOT_NAME}**\n\n"
        "مرحباً بك في لوحة الإدارة.\n"
        "مراقبة نشاط السوق، وإدارة المستخدمين، والتحكم بحالة الصيانة:\n\n"
        f"🛠️ **حالة الصيانة:** {m_status_ar}\n\n"
        "اختر الإجراء المطلوب من القائمة:"
    )
    await message.answer(text, reply_markup=get_admin_main_kb(is_m), parse_mode="Markdown")

@admin_router.callback_query(F.data == "admin_main_menu")
async def cq_admin_main_menu(callback: CallbackQuery, db_session: AsyncSession, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك بالوصول.", show_alert=True)
        return
    await state.clear()

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

    m_status_ar = "🟢 مفعل (الوصول محصور بالأدمن)" if is_m else "🔴 معطل (الوصول متاح للجميع)"

    text = (
        f"👑 **لوحة تحكم {BOT_NAME}**\n\n"
        "مرحباً بك في لوحة الإدارة.\n"
        "مراقبة نشاط السوق، وإدارة المستخدمين، والتحكم بحالة الصيانة:\n\n"
        f"🛠️ **حالة الصيانة:** {m_status_ar}\n\n"
        "اختر الإجراء المطلوب من القائمة:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=get_admin_main_kb(is_m), parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=get_admin_main_kb(is_m), parse_mode="Markdown")
    await callback.answer()

async def get_stats_text(db_session: AsyncSession) -> str:
    total_users_res = await db_session.execute(select(func.count()).select_from(User))
    total_users = total_users_res.scalar_one() or 0

    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    deals_all_res = await db_session.execute(
        select(Deal.status, func.count()).group_by(Deal.status)
    )
    deals_all_map = {status.upper() if status else "UNKNOWN": count for status, count in deals_all_res.all()}
    
    comp_all = deals_all_map.get("COMPLETED", 0)
    exp_all = deals_all_map.get("EXPIRED", 0)
    canc_all = deals_all_map.get("CANCELLED", 0)
    total_closed_all = comp_all + exp_all + canc_all
    
    deals_month_res = await db_session.execute(
        select(Deal.status, func.count())
        .where(Deal.created_at >= month_start)
        .group_by(Deal.status)
    )
    deals_month_map = {status.upper() if status else "UNKNOWN": count for status, count in deals_month_res.all()}

    comp_month = deals_month_map.get("COMPLETED", 0)
    exp_month = deals_month_map.get("EXPIRED", 0)
    canc_month = deals_month_map.get("CANCELLED", 0)
    total_closed_month = comp_month + exp_month + canc_month

    def calc_pct(count: int, total: int) -> str:
        if total <= 0:
            return "0.0%"
        return f"{(count / total * 100):.1f}%"

    profit_res = await db_session.execute(
        select(func.coalesce(func.sum(Deal.fee_usdt), 0)).where(func.upper(Deal.status) == "COMPLETED")
    )
    total_net_profit = profit_res.scalar_one() or Decimal("0")

    profit_month_res = await db_session.execute(
        select(func.coalesce(func.sum(Deal.fee_usdt), 0))
        .where(func.upper(Deal.status) == "COMPLETED")
        .where(Deal.created_at >= month_start)
    )
    month_net_profit = profit_month_res.scalar_one() or Decimal("0")

    return (
        f"📊 **إحصائيات وتقارير {BOT_NAME}**\n\n\n"
        "👥 **إجمالي المستخدمين المسجلين:**\n"
        f"• `{total_users:,}` مستخدم\n\n\n"
        "💰 **صافي أرباح العمولات (الصفقات المكتملة):**\n\n"
        f"• الإجمالي العام:  `{format_usdt(total_net_profit)}` USDT\n"
        f"• الشهر الحالي:  `{format_usdt(month_net_profit)}` USDT\n\n\n"
        "📅 **إحصائيات الشهر الحالي:**\n\n"
        f"• إجمالي الصفقات:  `{total_closed_month}` صفقة\n"
        f"  - مكتملة:  `{comp_month}` ({calc_pct(comp_month, total_closed_month)})\n"
        f"  - ملغاة:  `{canc_month}` ({calc_pct(canc_month, total_closed_month)})\n"
        f"  - منتهية الصلاحية:  `{exp_month}` ({calc_pct(exp_month, total_closed_month)})\n\n\n"
        "🌐 **الإحصائيات العامة التراكمية:**\n\n"
        f"• إجمالي الصفقات:  `{total_closed_all}` صفقة\n"
        f"  - مكتملة:  `{comp_all}` ({calc_pct(comp_all, total_closed_all)})\n"
        f"  - ملغاة:  `{canc_all}` ({calc_pct(canc_all, total_closed_all)})\n"
        f"  - منتهية الصلاحية:  `{exp_all}` ({calc_pct(exp_all, total_closed_all)})"
    )

@admin_router.message(Command("stats"))
async def cmd_admin_stats(message: Message, db_session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    stats_text = await get_stats_text(db_session)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 تحديث الأرقام", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🔙 العودة للوحة الإدارة", callback_data="admin_main_menu")]
    ])
    await message.answer(stats_text, reply_markup=kb, parse_mode="Markdown")

@admin_router.message(Command("search"))
async def cmd_admin_search(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminFSM.waiting_for_user_search_id)
    text = (
        "🔍 **الاستعلام عن مستخدم**\n\n\n"
        "أرسل معرّف تيليغرام الرقمي للمستخدم:\n"
        "*(مثال: `123456789`)*"
    )
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_main_menu")]
    ])
    sent_msg = await message.answer(text, reply_markup=cancel_kb, parse_mode="Markdown")
    await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)

@admin_router.message(Command("m_on"))
async def cmd_admin_m_on(message: Message, db_session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    result = await db_session.execute(
        select(SystemSetting).where(SystemSetting.key == "maintenance_mode").with_for_update()
    )
    setting = result.scalar_one_or_none()
    if not setting:
        setting = SystemSetting(key="maintenance_mode", value={"enabled": True})
        db_session.add(setting)
    else:
        setting.value = {"enabled": True}

    add_audit(
        session=db_session,
        action="MAINTENANCE_MODE_ON",
        target_type="system_setting",
        target_id="maintenance_mode",
        actor_id=message.from_user.id,
        actor_type="ADMIN",
        details={"enabled": True, "method": "command"}
    )
    await db_session.commit()
    set_cached_maintenance(True)

    await message.answer("🟢 **تم تفعيل وضع الصيانة بنجاح!**\nالوصول محصور بالأدمن فقط.", parse_mode="Markdown")

@admin_router.message(Command("m_off"))
async def cmd_admin_m_off(message: Message, db_session: AsyncSession):
    if not is_admin(message.from_user.id):
        return
    result = await db_session.execute(
        select(SystemSetting).where(SystemSetting.key == "maintenance_mode").with_for_update()
    )
    setting = result.scalar_one_or_none()
    if not setting:
        setting = SystemSetting(key="maintenance_mode", value={"enabled": False})
        db_session.add(setting)
    else:
        setting.value = {"enabled": False}

    add_audit(
        session=db_session,
        action="MAINTENANCE_MODE_OFF",
        target_type="system_setting",
        target_id="maintenance_mode",
        actor_id=message.from_user.id,
        actor_type="ADMIN",
        details={"enabled": False, "method": "command"}
    )
    await db_session.commit()
    set_cached_maintenance(False)

    await message.answer("🔴 **تم تعطيل وضع الصيانة بنجاح!**\nالوصول متاح لجميع المستخدمين الآن.", parse_mode="Markdown")

@admin_router.message(Command("broadcast"))
async def cmd_admin_broadcast(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.set_state(AdminFSM.waiting_for_broadcast_text)
    text = (
        "📢 **إرسال رسالة بث جماعي**\n\n\n"
        f"أرسل الآن نص الرسالة التي ترغب بإرسالها لجميع مستخدمي {BOT_NAME}:\n\n"
        "• تدعم الرسالة التنسيق المتقدم.\n"
        "• سيتم الإرسال لجميع الحسابات المسجلة بشكل فردي."
    )
    cancel_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_main_menu")]
    ])
    await message.answer(text, reply_markup=cancel_kb, parse_mode="Markdown")

# ================= 2. STATISTICS CALLBACK =================

@admin_router.callback_query(F.data == "admin_stats")
async def cq_admin_stats(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك بالوصول.", show_alert=True)
        return

    stats_text = await get_stats_text(db_session)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 تحديث الأرقام", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🔙 العودة للوحة الإدارة", callback_data="admin_main_menu")]
    ])

    try:
        await callback.message.edit_text(stats_text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(stats_text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

# ================= 3. USER MANAGEMENT & BAN TOGGLE =================

@admin_router.callback_query(F.data == "admin_search_user")
async def cq_admin_search_user(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك بالوصول.", show_alert=True)
        return

    await state.set_state(AdminFSM.waiting_for_user_search_id)
    text = (
        "🔍 **الاستعلام عن مستخدم**\n\n"
        "━━━━━━━━━━━━━━━\n"
        "أرسل معرّف تيليغرام الرقمي للمستخدم (أرقام فقط):\n"
        "━━━━━━━━━━━━━━━"
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

    text_input = message.text.strip() if message.text else ""
    if not text_input.isdigit():
        warn_msg = await message.answer(
            "⚠️ **معرف غير صالح!** يرجى إرسال أرقام فقط:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ إلغاء", callback_data="admin_main_menu")]
            ]),
            parse_mode="Markdown"
        )
        await state.update_data(prompt_msg_id=warn_msg.message_id, prompt_chat_id=warn_msg.chat.id)
        return

    target_id = int(text_input)
    result = await db_session.execute(
        select(User).where(User.telegram_id == target_id)
    )
    user = result.scalar_one_or_none()

    if not user:
        await message.answer(
            f"❌ لم يتم العثور على أي مستخدم مسجل بالمعرف `{target_id}` في قاعدة البيانات.",
            reply_markup=get_admin_back_kb(),
            parse_mode="Markdown"
        )
        await state.clear()
        return

    await state.clear()
    await send_user_card(message, user)

async def send_user_card(event_or_msg, user: User, is_edit: bool = False):
    ban_badge = "🚫 محظور" if user.is_banned else "✅ نشط ومصرح"
    uname = safe_username(user.username, user.telegram_id)
    reg_date = format_local_datetime(user.created_at) if user.created_at else "غير محدد"

    card_text = (
        f"👤 **ملف المستخدم:**  `{user.telegram_id}`\n\n\n"
        f"• اسم المستخدم:  {uname}\n\n"
        f"• المعرف الرقمي:  `{user.telegram_id}`\n\n"
        f"• تاريخ التسجيل:  `{reg_date}`\n\n"
        f"• الصفقات المكتملة:  `{user.completed_deals_count}` صفقة\n\n"
        f"• تقييم السمعة:  ⭐ `{user.reputation_score}` من 5.0\n\n"
        f"• حالة الحساب:  {ban_badge}"
    )

    toggle_btn_text = "✅ فك الحظر عن المستخدم" if user.is_banned else "🚫 حظر المستخدم فوراً"
    action = "unban" if user.is_banned else "ban"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=toggle_btn_text, callback_data=f"admin_toggle_ban_{action}_{user.telegram_id}")],
        [InlineKeyboardButton(text="🔍 بحث عن مستخدم آخر", callback_data="admin_search_user")],
        [InlineKeyboardButton(text="🔙 العودة للوحة الإدارة", callback_data="admin_main_menu")]
    ])

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
    action = parts[3] # 'ban' or 'unban'
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
        msg = f"تم حظر المستخدم {target_id} بنجاح."
    else:
        user.is_banned = False
        msg = f"تم إلغاء حظر المستخدم {target_id} بنجاح."

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
    await send_user_card(callback, user, is_edit=True)

# ================= 4. BROADCAST SYSTEM =================

@admin_router.callback_query(F.data == "admin_broadcast")
async def cq_admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك بالوصول.", show_alert=True)
        return

    await state.set_state(AdminFSM.waiting_for_broadcast_text)
    text = (
        "📢 **إرسال رسالة بث عام عبر Alpha Bot**\n\n"
        "━━━━━━━━━━━━━━━\n"
        "أرسل نص الرسالة المطلوب تعميمها على مستخدمي البوت.\n\n"
        "• تدعم الرسالة التنسيق.\n"
        "• يتم الإرسال لكافة الحسابات المسجلة تباعاً.\n"
        "━━━━━━━━━━━━━━━"
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

    # Fetch all users
    users_res = await db_session.execute(select(User.telegram_id))
    all_user_ids = users_res.scalars().all()
    total_recipients = len(all_user_ids)

    status_msg = await message.answer(
        f"⏳ جاري بدء إرسال البث لـ `{total_recipients}` مستخدم...\nيرجى الانتظار...",
        parse_mode="Markdown"
    )

    success_count = 0
    fail_count = 0

    formatted_broadcast = (
        "📢 **تنبيه إداري — Alpha Bot**\n\n"
        "━━━━━━━━━━━━━━━\n"
        f"{broadcast_text}\n"
        "━━━━━━━━━━━━━━━"
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
        # Safe rate limit delay (max 25 messages per second to strictly respect Telegram's 30/s limit)
        await asyncio.sleep(0.04)

    summary_text = (
        "✅ **اكتملت عملية البث الجماعي**\n\n"
        "━━━━━━━━━━━━━━━\n"
        f"• إجمالي المستهدفين: `{total_recipients}`\n"
        f"• ✅ تم الإرسال بنجاح: `{success_count}`\n"
        f"• ❌ فشل الإرسال (حظر البوت أو حساب ملغى): `{fail_count}`\n"
        "━━━━━━━━━━━━━━━"
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

# ================= 5. MAINTENANCE MODE TOGGLE =================

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

    # Re-render main admin menu
    m_status_ar = "🟢 مفعل (الوصول محصور بالأدمن)" if new_val else "🔴 معطل (الوصول متاح للجميع)"
    text = (
        f"👑 **لوحة تحكم {BOT_NAME}**\n\n"
        "مرحباً بك في لوحة الإدارة.\n"
        "مراقبة نشاط السوق، وإدارة المستخدمين، والتحكم بحالة الصيانة:\n\n"
        f"🛠️ **حالة الصيانة:** {m_status_ar}\n\n"
        "اختر الإجراء المطلوب من القائمة:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=get_admin_main_kb(new_val), parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=get_admin_main_kb(new_val), parse_mode="Markdown")


# ================= 6. DEALS OVERSIGHT: /pending & /deal =================

@admin_router.message(Command("pending"))
async def cmd_admin_pending(message: Message, db_session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    result = await db_session.execute(
        select(Deal).where(Deal.status == "ADMIN_APPROVAL").order_by(Deal.created_at.desc())
    )
    pending_deals = result.scalars().all()

    if not pending_deals:
        await message.answer(
            "✅ **لا توجد أي صفقات معلقة بانتظار الاعتماد حالياً.**\n\nكافة الصفقات تم تدقيقها وإطلاقها.",
            parse_mode="Markdown"
        )
        return

    await message.answer(
        f"🛡️ **الصفقات المعلقة بانتظار الاعتماد الإداري:**  `{len(pending_deals)}` صفقة\n\n"
        "يمكنك مراجعة كل صفقة واعتماد إطلاق الرصيد فوراً:",
        parse_mode="Markdown"
    )

    for deal in pending_deals:
        admin_text = await format_admin_deal_review_text(deal, db_session)
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 اعتماد وإطلاق الضمان", callback_data=f"ask_rel_{deal.id}")],
            [InlineKeyboardButton(text="🔍 فحص تفصيلي للصفقة", callback_data=f"adm_inspect_deal_{deal.id}")]
        ])
        await message.answer(admin_text, reply_markup=admin_kb, parse_mode="Markdown")


@admin_router.message(Command("deal"))
async def cmd_admin_inspect_deal(message: Message, db_session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()
    if len(args) < 2 or not args[1].isdigit():
        await message.answer(
            "⚠️ **طريقة الاستخدام:**\n`/deal [رقم_الصفقة]`\n\n*(مثال: `/deal 15`)*",
            parse_mode="Markdown"
        )
        return

    deal_id = int(args[1])
    result = await db_session.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()

    if not deal:
        await message.answer(f"❌ لم يتم العثور على أي صفقة بالمعرف `#{deal_id}`.", parse_mode="Markdown")
        return

    admin_text = await format_admin_deal_review_text(deal, db_session)
    buttons = []
    if deal.status == "ADMIN_APPROVAL":
        buttons.append([InlineKeyboardButton(text="🚀 اعتماد وإطلاق الضمان", callback_data=f"ask_rel_{deal.id}")])
    buttons.append([InlineKeyboardButton(text="🔄 تحديث البيانات", callback_data=f"adm_inspect_deal_{deal.id}")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
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
    buttons = []
    if deal.status == "ADMIN_APPROVAL":
        buttons.append([InlineKeyboardButton(text="🚀 اعتماد وإطلاق الضمان", callback_data=f"ask_rel_{deal.id}")])
    buttons.append([InlineKeyboardButton(text="🔄 تحديث البيانات", callback_data=f"adm_inspect_deal_{deal.id}")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    try:
        await callback.message.edit_text(admin_text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(admin_text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


# ================= 7. MARKET INSIGHTS: /market =================

@admin_router.message(Command("market"))
async def cmd_admin_market(message: Message, db_session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    # 1. Total Volume of completed deals (All-Time)
    vol_all_res = await db_session.execute(
        select(
            func.coalesce(func.sum(Deal.amount_usdt), 0),
            func.coalesce(func.sum(Deal.amount_syp), 0),
            func.count(Deal.id)
        ).where(func.upper(Deal.status) == "COMPLETED")
    )
    total_usdt_vol, total_syp_vol, total_comp_count = vol_all_res.one()

    # 2. Total Volume of completed deals (This Month)
    vol_month_res = await db_session.execute(
        select(
            func.coalesce(func.sum(Deal.amount_usdt), 0),
            func.coalesce(func.sum(Deal.amount_syp), 0),
            func.count(Deal.id)
        ).where(func.upper(Deal.status) == "COMPLETED", Deal.created_at >= month_start)
    )
    month_usdt_vol, month_syp_vol, month_comp_count = vol_month_res.one()

    # 3. Active Offers in the Orderbook right now
    active_offers_res = await db_session.execute(
        select(
            Offer.offer_type,
            func.count(Offer.id),
            func.coalesce(func.sum(Offer.max_limit), 0),
            func.coalesce(func.avg(Offer.rate), 0)
        ).where(Offer.status.in_(["active", "negotiating"]))
        .group_by(Offer.offer_type)
    )
    active_summary = {t: (cnt, sum_max, avg_rate) for t, cnt, sum_max, avg_rate in active_offers_res.all()}

    sell_cnt, sell_usdt_pool, sell_avg_rate = active_summary.get("sell", (0, Decimal("0"), Decimal("0")))
    buy_cnt, buy_usdt_pool, buy_avg_rate = active_summary.get("buy", (0, Decimal("0"), Decimal("0")))

    # Average effective rate for completed deals
    avg_rate_all = (total_syp_vol / total_usdt_vol).quantize(Decimal("1")) if total_usdt_vol > 0 else Decimal("0")
    avg_rate_month = (month_syp_vol / month_usdt_vol).quantize(Decimal("1")) if month_usdt_vol > 0 else Decimal("0")

    market_text = (
        "📈 **تحليلات وبيانات السوق المالية**\n\n\n"
        "💵 **حجم التداول المالي المنفّذ (الصفقات المكتملة):**\n\n"
        f"• الإجمالي التراكمي:  `{format_usdt(total_usdt_vol)}` USDT\n"
        f"• الإجمالي بالليرة:  `{format_syp(total_syp_vol)}` SYP\n"
        f"• متوسط سعر التنفيذ العام:  `{format_syp(avg_rate_all)}` SYP\n"
        f"• عدد الصفقات المكتملة:  `{total_comp_count}` صفقة\n\n\n"
        "📅 **حجم التداول خلال الشهر الحالي:**\n\n"
        f"• حجم الشهر:  `{format_usdt(month_usdt_vol)}` USDT\n"
        f"• بالليرة:  `{format_syp(month_syp_vol)}` SYP\n"
        f"• متوسط سعر تنفيذ الشهر:  `{format_syp(avg_rate_month)}` SYP\n"
        f"• صفقات هذا الشهر:  `{month_comp_count}` صفقة\n\n\n"
        "📦 **عمق السوق والعروض النشطة حالياً:**\n\n"
        f"• عروض البيع النشطة:  `{sell_cnt}` عرض\n"
        f"  - حجم السيولة المعروضة للبيع:  `{format_usdt(sell_usdt_pool)}` USDT\n"
        f"  - متوسط سعر البيع المعروض:  `{format_syp(sell_avg_rate)}` SYP\n\n"
        f"• عروض الشراء النشطة:  `{buy_cnt}` عرض\n"
        f"  - حجم الطلب المعروض للشراء:  `{format_usdt(buy_usdt_pool)}` USDT\n"
        f"  - متوسط سعر الشراء المطلوب:  `{format_syp(buy_avg_rate)}` SYP"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 تحديث التحليلات", callback_data="adm_refresh_market")],
        [InlineKeyboardButton(text="🔙 العودة للوحة الإدارة", callback_data="admin_main_menu")]
    ])
    await message.answer(market_text, reply_markup=kb, parse_mode="Markdown")


@admin_router.callback_query(F.data == "adm_refresh_market")
async def cq_admin_refresh_market(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك بالوصول.", show_alert=True)
        return

    now = datetime.now(timezone.utc)
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)

    vol_all_res = await db_session.execute(
        select(
            func.coalesce(func.sum(Deal.amount_usdt), 0),
            func.coalesce(func.sum(Deal.amount_syp), 0),
            func.count(Deal.id)
        ).where(func.upper(Deal.status) == "COMPLETED")
    )
    total_usdt_vol, total_syp_vol, total_comp_count = vol_all_res.one()

    vol_month_res = await db_session.execute(
        select(
            func.coalesce(func.sum(Deal.amount_usdt), 0),
            func.coalesce(func.sum(Deal.amount_syp), 0),
            func.count(Deal.id)
        ).where(func.upper(Deal.status) == "COMPLETED", Deal.created_at >= month_start)
    )
    month_usdt_vol, month_syp_vol, month_comp_count = vol_month_res.one()

    active_offers_res = await db_session.execute(
        select(
            Offer.offer_type,
            func.count(Offer.id),
            func.coalesce(func.sum(Offer.max_limit), 0),
            func.coalesce(func.avg(Offer.rate), 0)
        ).where(Offer.status.in_(["active", "negotiating"]))
        .group_by(Offer.offer_type)
    )
    active_summary = {t: (cnt, sum_max, avg_rate) for t, cnt, sum_max, avg_rate in active_offers_res.all()}

    sell_cnt, sell_usdt_pool, sell_avg_rate = active_summary.get("sell", (0, Decimal("0"), Decimal("0")))
    buy_cnt, buy_usdt_pool, buy_avg_rate = active_summary.get("buy", (0, Decimal("0"), Decimal("0")))

    avg_rate_all = (total_syp_vol / total_usdt_vol).quantize(Decimal("1")) if total_usdt_vol > 0 else Decimal("0")
    avg_rate_month = (month_syp_vol / month_usdt_vol).quantize(Decimal("1")) if month_usdt_vol > 0 else Decimal("0")

    market_text = (
        "📈 **تحليلات وبيانات السوق المالية**\n\n\n"
        "💵 **حجم التداول المالي المنفّذ (الصفقات المكتملة):**\n\n"
        f"• الإجمالي التراكمي:  `{format_usdt(total_usdt_vol)}` USDT\n"
        f"• الإجمالي بالليرة:  `{format_syp(total_syp_vol)}` SYP\n"
        f"• متوسط سعر التنفيذ العام:  `{format_syp(avg_rate_all)}` SYP\n"
        f"• عدد الصفقات المكتملة:  `{total_comp_count}` صفقة\n\n\n"
        "📅 **حجم التداول خلال الشهر الحالي:**\n\n"
        f"• حجم الشهر:  `{format_usdt(month_usdt_vol)}` USDT\n"
        f"• بالليرة:  `{format_syp(month_syp_vol)}` SYP\n"
        f"• متوسط سعر تنفيذ الشهر:  `{format_syp(avg_rate_month)}` SYP\n"
        f"• صفقات هذا الشهر:  `{month_comp_count}` صفقة\n\n\n"
        "📦 **عمق السوق والعروض النشطة حالياً:**\n\n"
        f"• عروض البيع النشطة:  `{sell_cnt}` عرض\n"
        f"  - حجم السيولة المعروضة للبيع:  `{format_usdt(sell_usdt_pool)}` USDT\n"
        f"  - متوسط سعر البيع المعروض:  `{format_syp(sell_avg_rate)}` SYP\n\n"
        f"• عروض الشراء النشطة:  `{buy_cnt}` عرض\n"
        f"  - حجم الطلب المعروض للشراء:  `{format_usdt(buy_usdt_pool)}` USDT\n"
        f"  - متوسط سعر الشراء المطلوب:  `{format_syp(buy_avg_rate)}` SYP"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 تحديث التحليلات", callback_data="adm_refresh_market")],
        [InlineKeyboardButton(text="🔙 العودة للوحة الإدارة", callback_data="admin_main_menu")]
    ])

    try:
        await callback.message.edit_text(market_text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(market_text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


# ================= 8. TOP TRADERS: /top_traders =================

@admin_router.message(Command("top_traders"))
async def cmd_admin_top_traders(message: Message, db_session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    result = await db_session.execute(
        select(User)
        .order_by(User.completed_deals_count.desc(), User.reputation_score.desc())
        .limit(10)
    )
    top_users = result.scalars().all()

    if not top_users:
        await message.answer("ℹ️ لا يوجد مستخدمين مسجلين بعد.", parse_mode="Markdown")
        return

    lines = [
        "🏆 **قائمة كبار المتداولين الأكثر نشاطاً وموثوقية:**\n\n"
    ]

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    for idx, u in enumerate(top_users):
        medal = medals[idx] if idx < len(medals) else "👤"
        uname = f"@{u.username}" if u.username else "بدون معرف"
        ban_tag = " [محظور 🚫]" if u.is_banned else ""
        lines.append(
            f"{medal} **{uname}** (`{u.telegram_id}`){ban_tag}\n"
            f"• الصفقات المكتملة:  `{u.completed_deals_count}` صفقة\n"
            f"• تقييم السمعة:  ⭐ `{u.reputation_score}` / 5.0\n"
        )

    lines.append("لإدارة أي مستخدم، استخدم الأمر: `/search [معرف_المستخدم]`")
    await message.answer("\n".join(lines), parse_mode="Markdown")


# ================= 9. DISPUTES & DIRECT REPLY: /disputes & /reply =================

@admin_router.message(Command("disputes"))
async def cmd_admin_disputes(message: Message, db_session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    result = await db_session.execute(
        select(Dispute).where(Dispute.resolution.is_(None)).order_by(Dispute.id.desc())
    )
    open_disputes = result.scalars().all()

    if not open_disputes:
        await message.answer(
            "🛡️ **سجل النزاعات المالية:**\n\n\n"
            "✅ لا توجد أي نزاعات مفتوحة أو معلقة حالياً.\n"
            "كافة الصفقات تسير بشكل سلس وطبيعي.",
            parse_mode="Markdown"
        )
        return

    lines = [
        f"⚠️ **النزاعات المفتوحة والمعلقة:**  `{len(open_disputes)}` نزاع\n\n"
    ]
    for d in open_disputes:
        lines.append(
            f"📌 **نزاع #{d.id} — صفقة #{d.deal_id}**\n"
            f"• فُتح بواسطة:  `{d.opened_by}`\n"
            f"• السبب:  {d.reason or 'غير محدد'}\n"
            f"• للفحص والتدخل: `/deal {d.deal_id}`\n"
        )

    await message.answer("\n".join(lines), parse_mode="Markdown")


@admin_router.message(Command("reply"))
async def cmd_admin_reply_user(message: Message, bot: Bot):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split(maxsplit=2)
    if len(args) < 3 or not args[1].isdigit():
        await message.answer(
            "⚠️ **طريقة الاستخدام:**\n"
            "`/reply [معرف_المستخدم] [نص_الرد]`",
            parse_mode="Markdown"
        )
        return

    target_user_id = int(args[1])
    reply_content = args[2].strip()

    formatted_msg = (
        "📩 **رد رسمي من فريق إدارة منصة الضمان**\n\n\n"
        f"{reply_content}\n\n\n"
        "نتمنى لك تجربة تداول آمنة ومريحة! 🛡️"
    )

    try:
        await bot.send_message(chat_id=target_user_id, text=formatted_msg, parse_mode="Markdown")
        await message.answer(f"✅ تم إرسال الرد بنجاح إلى المستخدم `{target_user_id}`.", parse_mode="Markdown")
    except Exception as e:
        await message.answer(f"❌ تعذر إرسال الرسالة إلى `{target_user_id}`:\n`{e}`", parse_mode="Markdown")


# ================= 10. DATABASE BACKUP: /backup =================

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


# ================= 11. AUDIT TRAIL & SYSTEM LOGS: /audit =================

@admin_router.message(Command("audit"))
async def cmd_admin_audit(message: Message, db_session: AsyncSession):
    if not is_admin(message.from_user.id):
        return

    args = message.text.split()
    query = select(AuditLog).order_by(AuditLog.id.desc())

    # Optional filter: /audit deal 15 or /audit user 123456
    filter_label = "آخر العمليات الإدارية والمالية المسجلة"
    if len(args) >= 3:
        f_type = args[1].lower()
        f_id = args[2]
        query = query.where(AuditLog.target_type == f_type, AuditLog.target_id == f_id)
        filter_label = f"سجل العمليات الخاص بـ `{f_type}:{f_id}`"

    query = query.limit(10)
    result = await db_session.execute(query)
    logs = result.scalars().all()

    if not logs:
        await message.answer(
            f"📋 **سجل التدقيق الجنائي المالي (Audit Trail)**\n\n\n"
            f"لا توجد أي سجلات مدونة مطابقة لطلبك حتى الآن.",
            parse_mode="Markdown"
        )
        return

    lines = [
        f"📋 **سجل التدقيق الجنائي والعمليات الحساسة (Audit Trail)**\n\n",
        f"• الفلترة: {filter_label}\n\n",
        "━━━━━━━━━━━━━━━\n"
    ]

    action_names = {
        "RELEASE_ESCROW": "🚀 إطلاق ضمان مالي",
        "CANCEL_DEAL": "❌ إلغاء صفقة",
        "BAN_USER": "🚫 حظر مستخدم",
        "UNBAN_USER": "✅ فك حظر مستخدم",
        "MAINTENANCE_MODE_ON": "🟢 تفعيل الصيانة",
        "MAINTENANCE_MODE_OFF": "🔴 تعطيل الصيانة",
        "DISPUTE_RESOLVE": "⚖️ تسوية نزاع",
    }

    for log in logs:
        time_str = format_local_datetime(log.created_at) if log.created_at else "—"
        act_title = action_names.get(log.action, log.action)
        actor_tag = f"`{log.actor_id}`" if log.actor_id else "SYSTEM"
        lines.append(
            f"🔹 **#{log.id} — {act_title}**\n"
            f"• المنفّذ:  {log.actor_type} ({actor_tag})\n"
            f"• الهدف:  `{log.target_type}:{log.target_id}`\n"
            f"• التوقيت:  `{time_str}`\n"
            "━━━━━━━━━━━━━━━\n"
        )

    lines.append("\n💡 للتدقيق في صفقة محددة: `/audit deal [رقم_الصفقة]`")
    lines.append("📁 لتحميل ملف السجلات التقنية: اضغط الزر أدناه")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📁 تحميل ملف السجلات (escrow_system.log)", callback_data="adm_dl_system_log")],
        [InlineKeyboardButton(text="🔄 تحديث السجل", callback_data="adm_refresh_audit")]
    ])

    await message.answer("".join(lines), reply_markup=kb, parse_mode="Markdown")


@admin_router.callback_query(F.data == "adm_refresh_audit")
async def cq_admin_refresh_audit(callback: CallbackQuery, db_session: AsyncSession):
    if not is_admin(callback.from_user.id):
        await callback.answer("غير مصرح لك.", show_alert=True)
        return

    query = select(AuditLog).order_by(AuditLog.id.desc()).limit(10)
    result = await db_session.execute(query)
    logs = result.scalars().all()

    if not logs:
        await callback.answer("لا توجد سجلات بعد.")
        return

    action_names = {
        "RELEASE_ESCROW": "🚀 إطلاق ضمان مالي",
        "CANCEL_DEAL": "❌ إلغاء صفقة",
        "BAN_USER": "🚫 حظر مستخدم",
        "UNBAN_USER": "✅ فك حظر مستخدم",
        "MAINTENANCE_MODE_ON": "🟢 تفعيل الصيانة",
        "MAINTENANCE_MODE_OFF": "🔴 تعطيل الصيانة",
        "DISPUTE_RESOLVE": "⚖️ تسوية نزاع",
    }

    lines = [
        "📋 **سجل التدقيق الجنائي والعمليات الحساسة (Audit Trail)**\n\n",
        "• الفلترة: آخر العمليات الإدارية والمالية المسجلة\n\n",
        "━━━━━━━━━━━━━━━\n"
    ]
    for log in logs:
        time_str = format_local_datetime(log.created_at) if log.created_at else "—"
        act_title = action_names.get(log.action, log.action)
        actor_tag = f"`{log.actor_id}`" if log.actor_id else "SYSTEM"
        lines.append(
            f"🔹 **#{log.id} — {act_title}**\n"
            f"• المنفّذ:  {log.actor_type} ({actor_tag})\n"
            f"• الهدف:  `{log.target_type}:{log.target_id}`\n"
            f"• التوقيت:  `{time_str}`\n"
            "━━━━━━━━━━━━━━━\n"
        )

    lines.append("\n💡 للتدقيق في صفقة محددة: `/audit deal [رقم_الصفقة]`")
    lines.append("📁 لتحميل ملف السجلات التقنية: اضغط الزر أدناه")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📁 تحميل ملف السجلات (escrow_system.log)", callback_data="adm_dl_system_log")],
        [InlineKeyboardButton(text="🔄 تحديث السجل", callback_data="adm_refresh_audit")]
    ])

    try:
        await callback.message.edit_text("".join(lines), reply_markup=kb, parse_mode="Markdown")
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


