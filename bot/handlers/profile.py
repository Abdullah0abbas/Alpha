from decimal import Decimal
from datetime import datetime, timezone, timedelta
import re
from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func

from db.models import User, Deal, DealRating, KycRequest
from bot.states.flow_states import ProfileFSM
from bot.services.admin_notifier import dispatch_admin_notification
from bot.services.audit_service import add_audit
from bot.utils.formatters import escape_markdown, safe_username, format_usdt, format_local_datetime
from bot.utils.validators import validate_bep20_address, validate_payment_method_info
from bot.utils.payment_prompts import build_profile_pm_prompt, build_payment_method_selection_kb
from bot.utils.branding import BOT_NAME
from config.settings import settings

profile_router = Router()

_ARABIC_DIGITS_MAP = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def get_profile_keyboard(user: User) -> InlineKeyboardMarkup:
    """Builds clean, feature-rich interactive keyboard for user profile."""
    if not settings.REWARDS_ENABLED:
        buttons = [
            [
                InlineKeyboardButton(text="⭐ التقييم", callback_data="prof_reputation"),
                InlineKeyboardButton(text="⚡ صفقاتي", callback_data="main_my_deals")
            ],
            [
                InlineKeyboardButton(text="💵 عنوان USDT", callback_data="prof_wallet"),
                InlineKeyboardButton(text="💳 حساب الدفع", callback_data="prof_payment")
            ],
            [
                InlineKeyboardButton(text="🆔 توثيق الهوية", callback_data="prof_kyc"),
                InlineKeyboardButton(text="✏️ اللقب", callback_data="prof_set_nickname")
            ],
            [
                InlineKeyboardButton(text="🏠 الرئيسية", callback_data="menu_back_main")
            ]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    buttons = [
        [
            InlineKeyboardButton(text="⭐ التقييم", callback_data="prof_reputation"),
            InlineKeyboardButton(text="⚡ صفقاتي", callback_data="main_my_deals")
        ],
        [
            InlineKeyboardButton(text="💵 عنوان USDT", callback_data="prof_wallet"),
            InlineKeyboardButton(text="💳 حساب الدفع", callback_data="prof_payment")
        ],
        [
            InlineKeyboardButton(text="🆔 توثيق الهوية", callback_data="prof_kyc"),
            InlineKeyboardButton(text="🔗 الإحالة", callback_data="prof_referral")
        ],
        [
            InlineKeyboardButton(text="🌟 النقاط", callback_data="prof_rewards"),
            InlineKeyboardButton(text="✏️ اللقب", callback_data="prof_set_nickname")
        ],
        [
            InlineKeyboardButton(text="🏠 الرئيسية", callback_data="menu_back_main")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def build_profile_text(user: User, db_session: AsyncSession) -> str:
    """Builds the rich visual profile card."""
    user_display = safe_username(user.username, user.telegram_id)
    nickname_display = escape_markdown(user.nickname) if user.nickname else "غير محدد"
    
    vol_query = select(func.coalesce(func.sum(Deal.amount_usdt), 0)).where(
        (Deal.seller_id == user.telegram_id) | (Deal.buyer_id == user.telegram_id),
        Deal.status == "COMPLETED"
    )
    vol_res = await db_session.execute(vol_query)
    total_volume = vol_res.scalar() or Decimal("0")

    deals_count = user.completed_deals_count or 0
    rep_score = f"{user.reputation_score}%" if user.reputation_score is not None else "100%"
    
    if deals_count >= 50:
        tier_badge = "ذهبي 🥇"
    elif deals_count >= 10:
        tier_badge = "فضي 🥈"
    else:
        tier_badge = "برونزي 🥉"

    saved_wallet = escape_markdown(user.saved_usdt_address) if user.saved_usdt_address else "غير محدد"
    saved_acc = escape_markdown(user.saved_payment_info) if user.saved_payment_info else "غير محدد"

    kyc_badges = {
        "verified": "🟢 موثق ✅",
        "pending": "🟡 قيد المراجعة ⏳",
        "rejected": "🔴 مرفوض ❌",
        "unverified": "⚪ غير موثق (اختياري)"
    }
    kyc_display = kyc_badges.get(user.kyc_status or "unverified", "⚪ غير موثق (اختياري)")

    rewards_section = ""
    if settings.REWARDS_ENABLED:
        points = user.reward_points or 0
        rewards_section = f"🌟 **رصيد النقاط :** `{points}`\n"

    text = (
        "اختر ما تريد معرفته عن حسابك\n"
        f"ID : `{user.telegram_id}` ({user_display})\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👤 **اللقب :** `{nickname_display}`\n"
        f"🆔 **توثيق الهوية :** {kyc_display}\n"
        f"🏅 **المستوى :** {tier_badge}\n"
        f"📈 **الصفقات المكتملة :** `{deals_count}` صفقة\n"
        f"🧐 **السمعة :** ⭐️ `{rep_score}`\n"
        f"💰 **حجم التداول :** `{format_usdt(total_volume)} USDT`\n"
        f"{rewards_section}"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"💵 **عنوان USDT (BEP-20) :**\n`{saved_wallet}`\n"
        f"💳 **حساب الدفع (ل.س) :**\n`{saved_acc}`"
    )
    return text

@profile_router.message(Command("profile"))
@profile_router.callback_query(F.data == "menu_user_profile")
async def show_user_profile(event: Message | CallbackQuery, db_session: AsyncSession, state: FSMContext | None = None):
    if state:
        await state.clear()
    user_id = event.from_user.id
    res = await db_session.execute(select(User).where(User.telegram_id == user_id))
    user = res.scalar_one_or_none()
    
    if not user:
        user = User(
            telegram_id=user_id,
            username=event.from_user.username,
            reputation_score=Decimal("100"),
            completed_deals_count=0
        )
        db_session.add(user)
        await db_session.commit()

    text = await build_profile_text(user, db_session)
    kb = get_profile_keyboard(user)

    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            await event.message.answer(text, reply_markup=kb, parse_mode="Markdown")
        await event.answer()
    else:
        await event.answer(text, reply_markup=kb, parse_mode="Markdown")

# ================= 1. REPUTATION DETAILS =================
@profile_router.callback_query(F.data == "prof_reputation")
async def cq_prof_reputation(callback: CallbackQuery, db_session: AsyncSession):
    user_id = callback.from_user.id
    res = await db_session.execute(select(User).where(User.telegram_id == user_id))
    user = res.scalar_one_or_none()

    rep = user.reputation_score if user and user.reputation_score is not None else 100
    deals = user.completed_deals_count if user else 0

    rating_agg = await db_session.execute(
        select(func.count(DealRating.id), func.avg(DealRating.stars)).where(
            DealRating.target_id == user_id
        )
    )
    rating_count, avg_stars = rating_agg.one()
    rating_count = int(rating_count or 0)
    stars_line = (
        f"🌟 **التقييمات المستلمة:** `{rating_count}` تقييم (متوسط `{float(avg_stars):.1f}/5` ⭐)\n"
        if rating_count > 0
        else "🌟 **التقييمات المستلمة:** `0` تقييم\n"
    )

    text = (
        "⭐ **التقييم والسمعة**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"🧐 **السمعة :** ⭐️ `{rep}%`\n"
        f"{stars_line}"
        f"📈 **الصفقات المكتملة :** `{deals}` صفقة"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="menu_user_profile")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

# ================= 2. WALLET ADDRESS MANAGEMENT =================
@profile_router.callback_query(F.data == "prof_wallet")
async def cq_prof_wallet(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    user_id = callback.from_user.id
    res = await db_session.execute(select(User).where(User.telegram_id == user_id))
    user = res.scalar_one_or_none()
    
    current_addr = escape_markdown(user.saved_usdt_address) if user and user.saved_usdt_address else "غير محدد"

    await state.set_state(ProfileFSM.waiting_for_usdt_address)
    text = (
        f"📍 **العنوان الحالي :** `{current_addr}`\n\n"
        "💵 أرسل عنوان `USDT (BEP-20)` فقط، من دون أي نص أو كلمات إضافية."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="menu_user_profile")]
    ])
    try:
        sent_msg = await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    except Exception:
        sent_msg = await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    await callback.answer()

@profile_router.message(ProfileFSM.waiting_for_usdt_address)
async def process_saved_usdt(message: Message, state: FSMContext, db_session: AsyncSession):
    addr = message.text if message.text else ""
    is_valid, normalized_addr, err_msg = validate_bep20_address(addr)

    if not is_valid:
        warn_msg = await message.answer(
            f"⚠️ {err_msg}\n\n"
            "💵 أرسل عنوان `USDT (BEP-20)` فقط، من دون أي نص أو كلمات إضافية:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ إلغاء", callback_data="menu_user_profile")]
            ]),
            parse_mode="Markdown"
        )
        await state.update_data(prompt_msg_id=warn_msg.message_id, prompt_chat_id=warn_msg.chat.id)
        return

    res = await db_session.execute(select(User).where(User.telegram_id == message.from_user.id))
    user = res.scalar_one_or_none()
    if user:
        user.saved_usdt_address = normalized_addr
        await db_session.commit()

    await state.clear()
    await message.answer(
        "✅ **تم حفظ معلومات USDT:**\n"
        f"`{escape_markdown(normalized_addr)}`",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🪪 الملف الشخصي", callback_data="menu_user_profile")]
        ]),
        parse_mode="Markdown"
    )

# ================= 3. PAYMENT INFO MANAGEMENT =================

@profile_router.callback_query(F.data == "prof_payment")
async def cq_prof_payment(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    user_id = callback.from_user.id
    res = await db_session.execute(select(User).where(User.telegram_id == user_id))
    user = res.scalar_one_or_none()
    
    current_acc = escape_markdown(user.saved_payment_info) if user and user.saved_payment_info else "غير محدد"

    text = (
        "💳 **حساب استلام الليرة السورية**\n"
        f"الحساب الحالي : `{current_acc}`\n\n"
        "اختر وسيلة الدفع:"
    )
    kb = build_payment_method_selection_kb(
        callback_prefix="prof_pm_",
        back_text="🔙 رجوع",
        back_callback="menu_user_profile",
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@profile_router.callback_query(F.data.startswith("prof_pm_"))
async def cq_prof_choose_payment_method(callback: CallbackQuery, state: FSMContext):
    pm_key = callback.data.removeprefix("prof_pm_")
    prompt_res = build_profile_pm_prompt(pm_key)
    if not prompt_res:
        await callback.answer("طريقة دفع غير معروفة.", show_alert=True)
        return

    pm_name, prompt_text = prompt_res
    await state.set_state(ProfileFSM.waiting_for_payment_info)
    await state.update_data(selected_pm=pm_name)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="prof_payment")]
    ])
    try:
        sent_msg = await callback.message.edit_text(prompt_text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    except Exception:
        sent_msg = await callback.message.answer(prompt_text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    await callback.answer()

@profile_router.message(ProfileFSM.waiting_for_payment_info)
async def process_saved_payment_info(message: Message, state: FSMContext, db_session: AsyncSession):
    data = await state.get_data()
    selected_pm = data.get("selected_pm")
    raw_text = message.text.strip() if message.text else ""

    is_valid, formatted_info, err_msg = validate_payment_method_info(selected_pm, raw_text)

    if not is_valid:
        warn_msg = await message.answer(
            f"{err_msg}\n\nيرجى إعادة المحاولة أو الضغط على إلغاء:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ إلغاء", callback_data="prof_payment")]
            ]),
            parse_mode="Markdown"
        )
        await state.update_data(prompt_msg_id=warn_msg.message_id, prompt_chat_id=warn_msg.chat.id)
        return

    res = await db_session.execute(select(User).where(User.telegram_id == message.from_user.id))
    user = res.scalar_one_or_none()
    if user:
        user.saved_payment_info = formatted_info
        await db_session.commit()

    await state.clear()
    await message.answer(
        f"✅ **تم حفظ بيانات حساب الدفع بنجاح!**\n\n`{escape_markdown(formatted_info)}`",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🪪 العودة للملف الشخصي", callback_data="menu_user_profile")]
        ]),
        parse_mode="Markdown"
    )

# ================= 4. KYC & IDENTITY VERIFICATION =================

def _get_kyc_nav_kb(back_cb: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔙 رجوع", callback_data=back_cb),
            InlineKeyboardButton(text="❌ إلغاء", callback_data="menu_user_profile")
        ]
    ])


def _get_today_date_str() -> str:
    local_now = datetime.now(timezone.utc) + timedelta(hours=settings.DISPLAY_UTC_OFFSET_HOURS)
    return local_now.strftime("%Y-%m-%d")


@profile_router.callback_query(F.data == "prof_kyc")
async def cq_prof_kyc(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    await state.clear()
    user_id = callback.from_user.id
    res = await db_session.execute(select(User).where(User.telegram_id == user_id))
    user = res.scalar_one_or_none()

    status = (user.kyc_status if user else "unverified") or "unverified"

    last_req_res = await db_session.execute(
        select(KycRequest)
        .where(KycRequest.user_id == user_id)
        .order_by(KycRequest.id.desc())
        .limit(1)
    )
    last_req = last_req_res.scalar_one_or_none()

    buttons = []
    if status == "verified":
        desc = (
            f"🟢 **حسابك موثق رسمياً لدى {BOT_NAME} ✅**\n"
            "〰️〰️〰️〰️〰️〰️〰️\n"
            "تتمتع بشارة التاجر الموثوق وموثوقية عالية أمام المتداولين في السوق."
        )
        if last_req and last_req.status == "approved":
            desc += (
                f"\n\n▫️ **الاسم الموثق:** `{escape_markdown(last_req.full_name)}`\n"
                f"▫️ **تاريخ التوثيق:** `{format_local_datetime(last_req.reviewed_at or last_req.created_at)}`"
            )
    elif status == "pending":
        req_id_str = f" (`#{last_req.id}`)" if last_req else ""
        desc = (
            f"🟡 **طلب توثيق الهوية{req_id_str} قيد المراجعة الإدارية ⏳**\n"
            "〰️〰️〰️〰️〰️〰️〰️\n"
            "تم استلام بياناتك وصور الوثائق بنجاح، ويقوم فريق الإدارة بمراجعتها.\n"
            "سيصلك إشعار فوري هنا فور اعتماد التوثيق."
        )
    else:
        reject_block = ""
        if status == "rejected" and last_req and last_req.rejection_reason:
            reject_block = (
                f"🔴 **تم رفض طلبك السابق:**\n"
                f"📌 **السبب:** {escape_markdown(last_req.rejection_reason)}\n"
                "〰️〰️〰️〰️〰️〰️〰️\n"
            )
        desc = (
            f"{reject_block}"
            "⚪ **توثيق الهوية الشخصي (اختياري)**\n"
            "〰️〰️〰️〰️〰️〰️〰️\n"
            "يمنحك توثيق الهوية شارة **التاجر الموثق** ويزيد من ثقة المتداولين بعروضك وصفقاتك.\n\n"
            "📋 **المتطلبات لإتمام التوثيق (6 خطوات سريعة):**\n"
            "1️⃣ الاسم الثلاثي الكامل (كما في الهوية أو الجواز).\n"
            "2️⃣ الرقم الوطني.\n"
            "3️⃣ رقم الموبايل الشخصي.\n"
            "4️⃣ صورة الهوية الشخصية أو جواز السفر **من الأمام**.\n"
            "5️⃣ صورة الهوية الشخصية أو جواز السفر **من الخلف**.\n"
            f"6️⃣ **صورة سيلفي لك (الوجه واضح)** وأنت تمسك **الهوية + ورقة مكتوب عليها `{BOT_NAME}` وتاريخ اليوم**.\n\n"
            "🔒 *جميع البيانات والصور مشفرة وتُراجع حصراً من قِبل الإدارة ولا تظهر لأي مستخدم.*"
        )
        btn_label = "🔄 إعادة تقديم طلب توثيق الهوية" if status == "rejected" else "🚀 بدء توثيق الهوية الآن"
        buttons.append([InlineKeyboardButton(text=btn_label, callback_data="kyc_start_flow")])

    buttons.append([InlineKeyboardButton(text="🔙 العودة للملف الشخصي", callback_data="menu_user_profile")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)

    text = f"🆔 **نظام توثيق الهوية — {BOT_NAME}**\n\n{desc}"
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@profile_router.callback_query(F.data == "kyc_start_flow")
async def cq_kyc_start_flow(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    user_id = callback.from_user.id
    res = await db_session.execute(select(User).where(User.telegram_id == user_id))
    user = res.scalar_one_or_none()
    if user and user.kyc_status == "verified":
        await callback.answer("حسابك موثق بالفعل ✅", show_alert=True)
        return
    if user and user.kyc_status == "pending":
        await callback.answer("لديك طلب توثيق قيد المراجعة حالياً ⏳", show_alert=True)
        return

    await state.set_state(ProfileFSM.waiting_for_kyc_full_name)
    text = (
        "🆔 **توثيق الهوية — الخطوة 1 من 6**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        "👤 **الاسم الثلاثي الكامل:**\n\n"
        "أرسل اسمك الثلاثي الكامل كما هو مدون في الهوية الشخصية أو جواز السفر:"
    )
    kb = _get_kyc_nav_kb("prof_kyc")
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()


@profile_router.message(ProfileFSM.waiting_for_kyc_full_name)
async def process_kyc_full_name(message: Message, state: FSMContext):
    raw_name = " ".join((message.text or "").strip().split())
    words = [w for w in raw_name.split(" ") if len(w) >= 2]
    if len(words) < 3 or len(raw_name) > 100 or any(ch.isdigit() for ch in raw_name):
        await message.answer(
            "⚠️ **الاسم غير مكتمل**\n\n"
            "يرجى إدخال **الاسم الثلاثي الكامل** (3 كلمات على الأقل بدون أرقام) كما هو في الهوية:",
            reply_markup=_get_kyc_nav_kb("prof_kyc"),
            parse_mode="Markdown"
        )
        return

    await state.update_data(kyc_full_name=raw_name)
    await state.set_state(ProfileFSM.waiting_for_kyc_national_id)
    await message.answer(
        "🆔 **توثيق الهوية — الخطوة 2 من 6**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👤 **الاسم الثلاثي:** `{escape_markdown(raw_name)}`\n\n"
        "🔢 **الرقم الوطني:**\n"
        "أرسل الرقم الوطني المدون على هويتك الشخصية (أو رقم جواز السفر):",
        reply_markup=_get_kyc_nav_kb("kyc_back_to_name"),
        parse_mode="Markdown"
    )


@profile_router.callback_query(F.data == "kyc_back_to_name")
async def cq_kyc_back_to_name(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProfileFSM.waiting_for_kyc_full_name)
    text = (
        "🆔 **توثيق الهوية — الخطوة 1 من 6**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        "👤 **الاسم الثلاثي الكامل:**\n\n"
        "أرسل اسمك الثلاثي الكامل كما هو مدون في الهوية الشخصية أو جواز السفر:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=_get_kyc_nav_kb("prof_kyc"), parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=_get_kyc_nav_kb("prof_kyc"), parse_mode="Markdown")
    await callback.answer()


@profile_router.message(ProfileFSM.waiting_for_kyc_national_id)
async def process_kyc_national_id(message: Message, state: FSMContext):
    raw_nid = (message.text or "").strip().translate(_ARABIC_DIGITS_MAP)
    cleaned_nid = re.sub(r"[\s\-]", "", raw_nid)
    if not cleaned_nid or len(cleaned_nid) < 6 or len(cleaned_nid) > 25 or not re.match(r"^[A-Za-z0-9]+$", cleaned_nid):
        await message.answer(
            "⚠️ **الرقم الوطني غير صالح**\n\n"
            "يرجى إدخال الرقم الوطني بشكل صحيح (أرقام فقط أو رقم الجواز بدون رموز):",
            reply_markup=_get_kyc_nav_kb("kyc_back_to_name"),
            parse_mode="Markdown"
        )
        return

    data = await state.get_data()
    full_name = data.get("kyc_full_name", "")
    await state.update_data(kyc_national_id=cleaned_nid)
    await state.set_state(ProfileFSM.waiting_for_kyc_phone)
    await message.answer(
        "🆔 **توثيق الهوية — الخطوة 3 من 6**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👤 **الاسم الثلاثي:** `{escape_markdown(full_name)}`\n"
        f"🔢 **الرقم الوطني:** `{escape_markdown(cleaned_nid)}`\n\n"
        "📱 **رقم الموبايل:**\n"
        "أرسل رقم موبايلك الشخصي الفعال (مثال: `09xxxxxxxx`):",
        reply_markup=_get_kyc_nav_kb("kyc_back_to_nid"),
        parse_mode="Markdown"
    )


@profile_router.callback_query(F.data == "kyc_back_to_nid")
async def cq_kyc_back_to_nid(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    full_name = data.get("kyc_full_name", "")
    await state.set_state(ProfileFSM.waiting_for_kyc_national_id)
    text = (
        "🆔 **توثيق الهوية — الخطوة 2 من 6**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👤 **الاسم الثلاثي:** `{escape_markdown(full_name)}`\n\n"
        "🔢 **الرقم الوطني:**\n"
        "أرسل الرقم الوطني المدون على هويتك الشخصية (أو رقم جواز السفر):"
    )
    try:
        await callback.message.edit_text(text, reply_markup=_get_kyc_nav_kb("kyc_back_to_name"), parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=_get_kyc_nav_kb("kyc_back_to_name"), parse_mode="Markdown")
    await callback.answer()


@profile_router.message(ProfileFSM.waiting_for_kyc_phone)
async def process_kyc_phone(message: Message, state: FSMContext):
    raw_phone = (message.text or "").strip().translate(_ARABIC_DIGITS_MAP)
    cleaned_phone = re.sub(r"[\s\-\(\)]", "", raw_phone)
    if not re.match(r"^\+?\d{9,15}$", cleaned_phone):
        await message.answer(
            "⚠️ **رقم الموبايل غير صالح**\n\n"
            "يرجى إدخال رقم موبايل صحيح (مثال: `0912345678` أو مع رمز الدولة):",
            reply_markup=_get_kyc_nav_kb("kyc_back_to_nid"),
            parse_mode="Markdown"
        )
        return

    data = await state.get_data()
    full_name = data.get("kyc_full_name", "")
    nid = data.get("kyc_national_id", "")
    await state.update_data(kyc_phone=cleaned_phone)
    await state.set_state(ProfileFSM.waiting_for_kyc_id_front)
    await message.answer(
        "🆔 **توثيق الهوية — الخطوة 4 من 6**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👤 **الاسم الثلاثي:** `{escape_markdown(full_name)}`\n"
        f"🔢 **الرقم الوطني:** `{escape_markdown(nid)}`\n"
        f"📱 **رقم الموبايل:** `{escape_markdown(cleaned_phone)}`\n\n"
        "🪪 **صورة الهوية الشخصية أو جواز السفر (من الأمام):**\n"
        "أرسل الآن **صورة واضحة** للوجه الأمامي للهوية الشخصية أو جواز السفر (تأكد من وضوح الاسم والصورة والرقم):",
        reply_markup=_get_kyc_nav_kb("kyc_back_to_phone"),
        parse_mode="Markdown"
    )


@profile_router.callback_query(F.data == "kyc_back_to_phone")
async def cq_kyc_back_to_phone(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    full_name = data.get("kyc_full_name", "")
    nid = data.get("kyc_national_id", "")
    await state.set_state(ProfileFSM.waiting_for_kyc_phone)
    text = (
        "🆔 **توثيق الهوية — الخطوة 3 من 6**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👤 **الاسم الثلاثي:** `{escape_markdown(full_name)}`\n"
        f"🔢 **الرقم الوطني:** `{escape_markdown(nid)}`\n\n"
        "📱 **رقم الموبايل:**\n"
        "أرسل رقم موبايلك الشخصي الفعال (مثال: `09xxxxxxxx`):"
    )
    try:
        await callback.message.edit_text(text, reply_markup=_get_kyc_nav_kb("kyc_back_to_nid"), parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=_get_kyc_nav_kb("kyc_back_to_nid"), parse_mode="Markdown")
    await callback.answer()


@profile_router.message(ProfileFSM.waiting_for_kyc_id_front)
async def process_kyc_id_front(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer(
            "⚠️ **يرجى إرسال صورة (Photo) حصراً**\n\n"
            "أرسل صورة الوجه الأمامي للهوية الشخصية أو جواز السفر:",
            reply_markup=_get_kyc_nav_kb("kyc_back_to_phone"),
            parse_mode="Markdown"
        )
        return

    front_file_id = message.photo[-1].file_id
    await state.update_data(kyc_id_front_file_id=front_file_id)
    await state.set_state(ProfileFSM.waiting_for_kyc_id_back)
    await message.answer(
        "🆔 **توثيق الهوية — الخطوة 5 من 6**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        "✅ **تم استلام صورة الهوية من الأمام.**\n\n"
        "🪪 **صورة الهوية الشخصية أو جواز السفر (من الخلف):**\n"
        "أرسل الآن **صورة واضحة** للوجه الخلفي للهوية الشخصية (أو الصفحة المقابلة في جواز السفر):",
        reply_markup=_get_kyc_nav_kb("kyc_back_to_front"),
        parse_mode="Markdown"
    )


@profile_router.callback_query(F.data == "kyc_back_to_front")
async def cq_kyc_back_to_front(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    full_name = data.get("kyc_full_name", "")
    nid = data.get("kyc_national_id", "")
    phone = data.get("kyc_phone", "")
    await state.set_state(ProfileFSM.waiting_for_kyc_id_front)
    text = (
        "🆔 **توثيق الهوية — الخطوة 4 من 6**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👤 **الاسم الثلاثي:** `{escape_markdown(full_name)}`\n"
        f"🔢 **الرقم الوطني:** `{escape_markdown(nid)}`\n"
        f"📱 **رقم الموبايل:** `{escape_markdown(phone)}`\n\n"
        "🪪 **صورة الهوية الشخصية أو جواز السفر (من الأمام):**\n"
        "أرسل الآن **صورة واضحة** للوجه الأمامي للهوية الشخصية أو جواز السفر:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=_get_kyc_nav_kb("kyc_back_to_phone"), parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=_get_kyc_nav_kb("kyc_back_to_phone"), parse_mode="Markdown")
    await callback.answer()


@profile_router.message(ProfileFSM.waiting_for_kyc_id_back)
async def process_kyc_id_back(message: Message, state: FSMContext):
    if not message.photo:
        await message.answer(
            "⚠️ **يرجى إرسال صورة (Photo) حصراً**\n\n"
            "أرسل صورة الوجه الخلفي للهوية الشخصية أو جواز السفر:",
            reply_markup=_get_kyc_nav_kb("kyc_back_to_front"),
            parse_mode="Markdown"
        )
        return

    back_file_id = message.photo[-1].file_id
    today_str = _get_today_date_str()
    await state.update_data(kyc_id_back_file_id=back_file_id)
    await state.set_state(ProfileFSM.waiting_for_kyc_selfie)
    await message.answer(
        "🆔 **توثيق الهوية — الخطوة 6 من 6 (الأخيرة)**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        "✅ **تم استلام صورة الهوية من الخلف.**\n\n"
        "🤳 **صورة شخصية (سيلفي التوثيق):**\n"
        "أرسل الآن **صورة لك يظهر فيها وجهك بوضوح** وأنت تمسك بيدك:\n"
        f"1️⃣ **ورقة مكتوب عليها بخط اليد:**\n"
        f"   • `{BOT_NAME}`\n"
        f"   • تاريخ اليوم: `{today_str}`\n"
        "2️⃣ **هويتك الشخصية أو جواز سفرك** بجانب الورقة.\n\n"
        "⚠️ *تأكد أن الوجه والكتابة على الورقة والهوية واضحة تماماً في الصورة.*",
        reply_markup=_get_kyc_nav_kb("kyc_back_to_back"),
        parse_mode="Markdown"
    )


@profile_router.callback_query(F.data == "kyc_back_to_back")
async def cq_kyc_back_to_back(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProfileFSM.waiting_for_kyc_id_back)
    text = (
        "🆔 **توثيق الهوية — الخطوة 5 من 6**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        "🪪 **صورة الهوية الشخصية أو جواز السفر (من الخلف):**\n"
        "أرسل الآن **صورة واضحة** للوجه الخلفي للهوية الشخصية أو جواز السفر:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=_get_kyc_nav_kb("kyc_back_to_front"), parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=_get_kyc_nav_kb("kyc_back_to_front"), parse_mode="Markdown")
    await callback.answer()


@profile_router.message(ProfileFSM.waiting_for_kyc_selfie)
async def process_kyc_selfie(message: Message, state: FSMContext):
    if not message.photo:
        today_str = _get_today_date_str()
        await message.answer(
            "⚠️ **يرجى إرسال صورة (Photo) حصراً**\n\n"
            f"أرسل صورة يظهر فيها وجهك بوضوح وأنت تمسك الهوية مع ورقة مكتوب عليها `{BOT_NAME}` وتاريخ اليوم (`{today_str}`):",
            reply_markup=_get_kyc_nav_kb("kyc_back_to_back"),
            parse_mode="Markdown"
        )
        return

    selfie_file_id = message.photo[-1].file_id
    await state.update_data(kyc_selfie_file_id=selfie_file_id)
    await state.set_state(ProfileFSM.confirm_kyc_submission)

    data = await state.get_data()
    full_name = data.get("kyc_full_name", "")
    nid = data.get("kyc_national_id", "")
    phone = data.get("kyc_phone", "")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ إرسال طلب التوثيق للإدارة", callback_data="kyc_submit_final")],
        [
            InlineKeyboardButton(text="🔙 رجوع", callback_data="kyc_back_to_selfie"),
            InlineKeyboardButton(text="❌ إلغاء", callback_data="menu_user_profile")
        ]
    ])
    await message.answer(
        "📋 **مراجعة وتأكيد طلب توثيق الهوية**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👤 **الاسم الثلاثي:** `{escape_markdown(full_name)}`\n"
        f"🔢 **الرقم الوطني:** `{escape_markdown(nid)}`\n"
        f"📱 **رقم الموبايل:** `{escape_markdown(phone)}`\n"
        "🪪 **صورة الهوية (أمام):** مرفقة ✅\n"
        "🪪 **صورة الهوية (خلف):** مرفقة ✅\n"
        f"🤳 **صورة السيلفي مع الورقة ({BOT_NAME} + التاريخ) والهوية:** مرفقة ✅\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        "اضغط على **إرسال طلب التوثيق للإدارة** لإحالة ملفك للمراجعة:",
        reply_markup=kb,
        parse_mode="Markdown"
    )


@profile_router.callback_query(F.data == "kyc_back_to_selfie")
async def cq_kyc_back_to_selfie(callback: CallbackQuery, state: FSMContext):
    today_str = _get_today_date_str()
    await state.set_state(ProfileFSM.waiting_for_kyc_selfie)
    text = (
        "🆔 **توثيق الهوية — الخطوة 6 من 6 (الأخيرة)**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        "🤳 **صورة شخصية (سيلفي التوثيق):**\n"
        "أرسل الآن **صورة لك يظهر فيها وجهك بوضوح** وأنت تمسك بيدك:\n"
        f"1️⃣ **ورقة مكتوب عليها بخط اليد:** `{BOT_NAME}` وتاريخ اليوم `{today_str}`\n"
        "2️⃣ **هويتك الشخصية أو جواز سفرك** بجانب الورقة."
    )
    try:
        await callback.message.edit_text(text, reply_markup=_get_kyc_nav_kb("kyc_back_to_back"), parse_mode="Markdown")
    except Exception:
        await callback.message.answer(text, reply_markup=_get_kyc_nav_kb("kyc_back_to_back"), parse_mode="Markdown")
    await callback.answer()


@profile_router.callback_query(ProfileFSM.confirm_kyc_submission, F.data == "kyc_submit_final")
async def cq_kyc_submit_final(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession, bot: Bot):
    data = await state.get_data()
    if data.get("is_submitting_kyc"):
        await callback.answer("جاري إرسال الطلب...", show_alert=True)
        return
    await state.update_data(is_submitting_kyc=True)

    try:
        full_name = data.get("kyc_full_name")
        nid = data.get("kyc_national_id")
        phone = data.get("kyc_phone")
        front_fid = data.get("kyc_id_front_file_id")
        back_fid = data.get("kyc_id_back_file_id")
        selfie_fid = data.get("kyc_selfie_file_id")

        if not all([full_name, nid, phone, front_fid, back_fid, selfie_fid]):
            await state.clear()
            await callback.answer("⚠️ بيانات الطلب غير مكتملة، يرجى المحاولة من جديد.", show_alert=True)
            return

        user_id = callback.from_user.id
        user_res = await db_session.execute(
            select(User).where(User.telegram_id == user_id).with_for_update()
        )
        user = user_res.scalar_one_or_none()
        if not user:
            user = User(
                telegram_id=user_id,
                username=callback.from_user.username,
                reputation_score=Decimal("100"),
                completed_deals_count=0
            )
            db_session.add(user)
            await db_session.flush()

        if user.kyc_status == "verified":
            await db_session.commit()
            await state.clear()
            await callback.answer("حسابك موثق بالفعل ✅", show_alert=True)
            return

        if user.kyc_status == "pending":
            await db_session.commit()
            await state.clear()
            await callback.answer("لديك طلب توثيق قيد المراجعة حالياً ⏳", show_alert=True)
            return

        kyc_req = KycRequest(
            user_id=user_id,
            full_name=full_name,
            national_id=nid,
            phone_number=phone,
            id_front_file_id=front_fid,
            id_back_file_id=back_fid,
            selfie_file_id=selfie_fid,
            status="pending"
        )
        db_session.add(kyc_req)
        user.kyc_status = "pending"
        await db_session.flush()

        add_audit(
            session=db_session,
            action="USER_SUBMIT_KYC",
            target_type="user",
            target_id=str(user_id),
            actor_id=user_id,
            actor_type="USER",
            details={
                "kyc_request_id": kyc_req.id,
                "user_id": user_id,
            }
        )
        await db_session.commit()
        await db_session.refresh(kyc_req)
        await state.clear()
    finally:
        if await state.get_state() is not None:
            await state.update_data(is_submitting_kyc=False)

    user_tag = safe_username(user.username or callback.from_user.username, user_id)
    user_nick = escape_markdown(user.nickname) if user.nickname else "—"
    today_str = _get_today_date_str()

    admin_card_text = (
        f"🆔 **طلب توثيق هوية جديد (KYC) — طلب #{kyc_req.id}**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👤 **المستخدم:** `{user_id}` ({user_tag} | اللقب: {user_nick})\n"
        f"📝 **الاسم الثلاثي:** `{escape_markdown(full_name)}`\n"
        f"🔢 **الرقم الوطني:** `{escape_markdown(nid)}`\n"
        f"📱 **رقم الموبايل:** `{escape_markdown(phone)}`\n"
        f"📅 **تاريخ التقديم:** `{today_str}`\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        "📎 **الصور المرفقة أعلاه (3 صور):**\n"
        "1️⃣ الهوية/الجواز من الأمام\n"
        "2️⃣ الهوية/الجواز من الخلف\n"
        f"3️⃣ سيلفي الوجه مع الهوية وورقة (`{BOT_NAME}` + تاريخ اليوم)"
    )
    admin_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ قبول وتوثيق الحساب", callback_data=f"adm_kyc_app_{kyc_req.id}"),
            InlineKeyboardButton(text="❌ رفض مع ذكر السبب", callback_data=f"adm_kyc_rej_{kyc_req.id}")
        ],
        [
            InlineKeyboardButton(text=f"👤 ملف المستخدم {user_id}", callback_data=f"adm_view_user_{user_id}")
        ]
    ])
    media_photos = [
        (front_fid, f"1️⃣ صورة الهوية من الأمام — طلب #{kyc_req.id} (المستخدم `{user_id}`)"),
        (back_fid, f"2️⃣ صورة الهوية من الخلف — طلب #{kyc_req.id} (المستخدم `{user_id}`)"),
        (selfie_fid, f"3️⃣ سيلفي التوثيق ({BOT_NAME} + التاريخ + الهوية) — طلب #{kyc_req.id}"),
    ]

    await dispatch_admin_notification(
        bot=bot,
        category="kyc",
        text=admin_card_text,
        reply_markup=admin_kb,
        parse_mode="Markdown",
        media_group_photos=media_photos
    )

    done_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🪪 العودة للملف الشخصي", callback_data="menu_user_profile")]
    ])
    try:
        await callback.message.edit_text(
            f"✅ **تم إرسال طلب توثيق الهوية بنجاح — طلب #{kyc_req.id}**\n"
            "〰️〰️〰️〰️〰️〰️〰️\n"
            "تم إرسال بياناتك والصور الثلاث إلى الإدارة للمراجعة والتدقيق.\n"
            "سيصلك إشعار فوري هنا فور اعتماد توثيق حسابك.",
            reply_markup=done_kb,
            parse_mode="Markdown"
        )
    except Exception:
        await callback.message.answer(
            f"✅ **تم إرسال طلب توثيق الهوية بنجاح — طلب #{kyc_req.id}**\n"
            "سيصلك إشعار فوري هنا فور اعتماد توثيق حسابك.",
            reply_markup=done_kb,
            parse_mode="Markdown"
        )
    await callback.answer("تم إرسال طلب التوثيق بنجاح ✅")

# ================= 5. REFERRAL SYSTEM =================
@profile_router.callback_query(F.data == "prof_referral")
async def cq_prof_referral(callback: CallbackQuery):
    if not settings.REWARDS_ENABLED:
        await callback.answer("هذه الميزة غير متاحة حالياً.", show_alert=True)
        return

    bot_info = await callback.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{callback.from_user.id}"

    text = (
        "🔗 **برنامج الإحالة والمكافآت**\n\n"
        "شارك رابط الإحالة الخاص بك:\n\n"
        f"▫️ **رابطك الخاص:**\n`{ref_link}`\n\n"
        "▫️ تحصل على نقاط مكافآت عند تسجيل مستخدم جديد وإتمام أول صفقة ناجحة."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 العودة للملف الشخصي", callback_data="menu_user_profile")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

# ================= 6. GOLDEN POINTS PROGRAM =================
@profile_router.callback_query(F.data == "prof_rewards")
async def cq_prof_rewards(callback: CallbackQuery, db_session: AsyncSession):
    if not settings.REWARDS_ENABLED:
        await callback.answer("هذه الميزة غير متاحة حالياً.", show_alert=True)
        return

    user_id = callback.from_user.id
    res = await db_session.execute(select(User).where(User.telegram_id == user_id))
    user = res.scalar_one_or_none()
    points = user.reward_points if user else 0

    deals_count = user.completed_deals_count if user else 0
    if deals_count >= 50 or points >= 3000:
        level_str = "ماسي"
    elif deals_count >= 15 or points >= 1500:
        level_str = "ذهبي"
    else:
        level_str = "فضي"

    text = (
        "🌟 **برنامج النقاط والمكافآت**\n\n"
        f"▫️ **المستوى الحالي:** {level_str}\n"
        f"▫️ **رصيد النقاط:** `{points}` نقطة\n\n"
        "🎁 **جوائز الاستبدال:**\n"
        "▫️ `500` نقطة = `5 USDT`\n"
        "▫️ `1500` نقطة = `17 USDT`\n"
        "▫️ `3000` نقطة = `35 USDT`"
    )

    buttons = []
    if points >= 500:
        buttons.append([InlineKeyboardButton(text="استبدال 500 نقطة بـ 5 USDT", callback_data="claim_points_500")])
    if points >= 1500:
        buttons.append([InlineKeyboardButton(text="استبدال 1500 نقطة بـ 17 USDT", callback_data="claim_points_1500")])
    if points >= 3000:
        buttons.append([InlineKeyboardButton(text="استبدال 3000 نقطة بـ 35 USDT", callback_data="claim_points_3000")])

    buttons.append([InlineKeyboardButton(text="🔙 العودة للملف الشخصي", callback_data="menu_user_profile")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@profile_router.callback_query(F.data.startswith("claim_points_"))
async def cq_claim_points(callback: CallbackQuery, db_session: AsyncSession):
    if not settings.REWARDS_ENABLED:
        await callback.answer("هذه الميزة غير متاحة حالياً.", show_alert=True)
        return

    user_id = callback.from_user.id
    res = await db_session.execute(select(User).where(User.telegram_id == user_id))
    user = res.scalar_one_or_none()
    
    reward_map = {
        "claim_points_500": (500, 5),
        "claim_points_1500": (1500, 17),
        "claim_points_3000": (3000, 35),
    }
    action = callback.data
    if action not in reward_map:
        await callback.answer("طلب غير صالح.", show_alert=True)
        return

    req_points, reward_usdt = reward_map[action]
    current_points = user.reward_points if user else 0

    if current_points < req_points:
        await callback.answer("⚠️ رصيد نقاطك غير كافٍ للاستبدال.", show_alert=True)
        return

    user.reward_points -= req_points
    await db_session.commit()

    await callback.answer(f"تم تقديم طلب استبدال {reward_usdt} USDT بنجاح.", show_alert=True)
    
    admin_text = (
        "🎁 **طلب استبدال نقاط جديد**\n\n"
        f"▫️ **المستخدم:** {safe_username(user.username, user.telegram_id)}\n"
        f"▫️ **النقاط المستبدلة:** `{req_points}` نقطة\n"
        f"▫️ **المكافأة المستحقة:** `{reward_usdt} USDT`\n"
        f"▫️ **محفظة الاستلام:**\n`{escape_markdown(user.saved_usdt_address or 'غير محددة')}`"
    )
    for admin_id in settings.ADMIN_IDS:
        if admin_id:
            try:
                await callback.bot.send_message(chat_id=admin_id, text=admin_text, parse_mode="Markdown")
            except Exception:
                pass

    await cq_prof_rewards(callback, db_session)

# ================= 7. SET NICKNAME =================
@profile_router.callback_query(F.data == "prof_set_nickname")
async def cq_prof_set_nickname(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProfileFSM.waiting_for_nickname)
    text = (
        "✏️ **تغيير اللقب التجاري**\n\n"
        "أرسل الاسم أو اللقب التجاري الذي ترغب بظهوره للمتداولين الآخرين:\n"
        "*(الحد الأقصى: 30 حرفاً)*"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="menu_user_profile")]
    ])
    try:
        sent_msg = await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    except Exception:
        sent_msg = await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    await callback.answer()

@profile_router.message(ProfileFSM.waiting_for_nickname)
async def process_nickname(message: Message, state: FSMContext, db_session: AsyncSession):
    name = message.text.strip() if message.text else ""
    if len(name) < 2 or len(name) > 30:
        warn_msg = await message.answer(
            "⚠️ يرجى إدخال اسم بين 2 و 30 حرفاً:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ إلغاء", callback_data="menu_user_profile")]
            ]),
            parse_mode="Markdown"
        )
        await state.update_data(prompt_msg_id=warn_msg.message_id, prompt_chat_id=warn_msg.chat.id)
        return

    res = await db_session.execute(select(User).where(User.telegram_id == message.from_user.id))
    user = res.scalar_one_or_none()
    if user:
        user.nickname = name
        await db_session.commit()

    await state.clear()
    await message.answer(
        f"✅ **تم تحديث لقبك التجاري إلى:** `{escape_markdown(name)}`",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🪪 العودة للملف الشخصي", callback_data="menu_user_profile")]
        ]),
        parse_mode="Markdown"
    )
