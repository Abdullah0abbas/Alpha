from decimal import Decimal
from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import func

from db.models import User, Deal, Offer
from bot.states.flow_states import ProfileFSM
from bot.utils.formatters import escape_markdown, safe_username, format_usdt
from bot.utils.validators import validate_bep20_address, validate_payment_method_info
from bot.utils.payment_prompts import build_profile_pm_prompt, build_payment_method_selection_kb
from bot.utils.branding import BOT_NAME
from config.settings import settings

profile_router = Router()

def get_profile_keyboard(user: User) -> InlineKeyboardMarkup:
    """Builds clean, feature-rich interactive keyboard for user profile."""
    if not settings.REWARDS_ENABLED:
        buttons = [
            [
                InlineKeyboardButton(text="⭐ التقييم", callback_data="prof_reputation"),
                InlineKeyboardButton(text="🕒 الصفقات", callback_data="main_my_deals")
            ],
            [
                InlineKeyboardButton(text="💵 عنوان USDT", callback_data="prof_wallet"),
                InlineKeyboardButton(text="💳 حساب الدفع", callback_data="prof_payment")
            ],
            [
                InlineKeyboardButton(text="✏️ اللقب", callback_data="prof_set_nickname")
            ],
            [
                InlineKeyboardButton(text="🔙 الرئيسية", callback_data="menu_back_main")
            ]
        ]
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    buttons = [
        [
            InlineKeyboardButton(text="⭐ التقييم", callback_data="prof_reputation"),
            InlineKeyboardButton(text="🕒 الصفقات", callback_data="main_my_deals")
        ],
        [
            InlineKeyboardButton(text="💵 عنوان USDT", callback_data="prof_wallet"),
            InlineKeyboardButton(text="💳 حساب الدفع", callback_data="prof_payment")
        ],
        [
            InlineKeyboardButton(text="🆔 التوثيق", callback_data="prof_kyc"),
            InlineKeyboardButton(text="🔗 الإحالة", callback_data="prof_referral")
        ],
        [
            InlineKeyboardButton(text="🌟 النقاط", callback_data="prof_rewards"),
            InlineKeyboardButton(text="✏️ اللقب", callback_data="prof_set_nickname")
        ],
        [
            InlineKeyboardButton(text="🔙 الرئيسية", callback_data="menu_back_main")
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

    rewards_section = ""
    if settings.REWARDS_ENABLED:
        kyc_badges = {
            "verified": "🟢 موثق",
            "pending": "🟡 قيد المراجعة",
            "unverified": "⚪ غير موثق"
        }
        kyc_display = kyc_badges.get(user.kyc_status, "⚪ غير موثق")
        points = user.reward_points or 0
        rewards_section = (
            f"🆔 **التوثيق :** {kyc_display}\n"
            f"🌟 **رصيد النقاط :** `{points}`\n"
        )

    text = (
        "اختر ما تريد معرفته عن حسابك\n"
        f"ID : `{user.telegram_id}` ({user_display})\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👤 **اللقب :** `{nickname_display}`\n"
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
async def show_user_profile(event: Message | CallbackQuery, db_session: AsyncSession):
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

    text = (
        "⭐ **التقييم والسمعة**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"🧐 **السمعة :** ⭐️ `{rep}%`\n"
        f"📈 **الصفقات المكتملة :** `{deals}` صفقة\n"
        f"⚖️ **النزاعات المفتوحة :** `0`"
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
            [InlineKeyboardButton(text="👤 حسابي", callback_data="menu_user_profile")]
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
            [InlineKeyboardButton(text="👤 العودة للملف الشخصي", callback_data="menu_user_profile")]
        ]),
        parse_mode="Markdown"
    )

# ================= 4. KYC & VERIFICATION =================
@profile_router.callback_query(F.data == "prof_kyc")
async def cq_prof_kyc(callback: CallbackQuery, db_session: AsyncSession):
    if not settings.REWARDS_ENABLED:
        await callback.answer("هذه الميزة غير متاحة حالياً.", show_alert=True)
        return

    user_id = callback.from_user.id
    res = await db_session.execute(select(User).where(User.telegram_id == user_id))
    user = res.scalar_one_or_none()

    status = user.kyc_status if user else "unverified"
    if status == "verified":
        desc = f"🟢 **حسابك موثق بالكامل لدى {BOT_NAME}.**\nتتمتع بحدود تداول مفتوحة وشارة التاجر الموثوق."
    elif status == "pending":
        desc = "🟡 **طلب التوثيق قيد المراجعة الإدارية.**\nسيتم إشعارك فور اكتمال التدقيق."
    else:
        desc = (
            "⚪ **حسابك غير موثق حالياً.**\n\n"
            "مزايا التوثيق:\n"
            "▫️ شارة التوثيق بجانب اسمك في السوق.\n"
            "▫️ أولوية في تداول الصفقات والمبالغ الكبيرة.\n"
            "▫️ تعزيز موثوقية عروضك أمام المتداولين.\n\n"
            "لطلب التوثيق، تواصل مع فريق الإدارة عبر الدعم الفني."
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📞 التواصل مع الدعم للتوثيق", callback_data="supp_contact")],
        [InlineKeyboardButton(text="🔙 العودة للملف الشخصي", callback_data="menu_user_profile")]
    ])
    await callback.message.edit_text(
        f"🆔 **توثيق الحساب**\n\n{desc}",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await callback.answer()

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
            [InlineKeyboardButton(text="👤 العودة للملف الشخصي", callback_data="menu_user_profile")]
        ]),
        parse_mode="Markdown"
    )
