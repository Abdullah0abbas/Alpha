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
from bot.utils.validators import validate_bep20_address
from bot.utils.branding import BOT_NAME
from config.settings import settings

profile_router = Router()

def get_profile_keyboard(user: User) -> InlineKeyboardMarkup:
    """Builds clean, feature-rich interactive keyboard for user profile."""
    buttons = [
        [
            InlineKeyboardButton(text="⭐ سمعتي وتقييمي", callback_data="prof_reputation"),
            InlineKeyboardButton(text="🕒 سجل الصفقات", callback_data="main_my_deals")
        ],
        [
            InlineKeyboardButton(text="📍 عنوان USDT المحفوظ", callback_data="prof_wallet"),
            InlineKeyboardButton(text="💳 حساب الدفع المعتمد", callback_data="prof_payment")
        ],
        [
            InlineKeyboardButton(text="🆔 توثيق الحساب (KYC)", callback_data="prof_kyc"),
            InlineKeyboardButton(text="🔗 رابط الإحالة", callback_data="prof_referral")
        ],
        [
            InlineKeyboardButton(text="⚡ برنامج النقاط الذهبية", callback_data="prof_rewards"),
            InlineKeyboardButton(text="✏️ تعديل اللقب", callback_data="prof_set_nickname")
        ],
        [
            InlineKeyboardButton(text="🔙 العودة للرئيسية", callback_data="menu_back_main")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def build_profile_text(user: User, db_session: AsyncSession) -> str:
    """Builds the rich visual profile card."""
    user_display = safe_username(user.username, user.telegram_id)
    nickname_display = escape_markdown(user.nickname) if user.nickname else "غير محدد"
    
    # Calculate total trading volume from completed deals
    vol_query = select(func.coalesce(func.sum(Deal.amount_usdt), 0)).where(
        (Deal.seller_id == user.telegram_id) | (Deal.buyer_id == user.telegram_id),
        Deal.status == "COMPLETED"
    )
    vol_res = await db_session.execute(vol_query)
    total_volume = vol_res.scalar() or Decimal("0")

    deals_count = user.completed_deals_count or 0
    rep_score = f"{user.reputation_score}%" if user.reputation_score is not None else "100%"
    
    # Tier level badge
    if deals_count >= 50:
        tier_badge = "👑 تاجر ماسي (VIP Trader)"
    elif deals_count >= 15:
        tier_badge = "💎 تاجر محترف (Pro Trader)"
    elif deals_count >= 3:
        tier_badge = "🛡️ تاجر موثوق (Verified Trader)"
    else:
        tier_badge = "🌱 متداول مبتدئ (Beginner)"

    kyc_badges = {
        "verified": "🟢 موثّق بالكامل ✅",
        "pending": "🟡 قيد المراجعة الإدارية ⏳",
        "unverified": "⚪ غير موثّق"
    }
    kyc_display = kyc_badges.get(user.kyc_status, "⚪ غير موثّق")

    points = user.reward_points or 0
    saved_wallet = escape_markdown(user.saved_usdt_address) if user.saved_usdt_address else "لم يتم ضبطه بعد"
    saved_acc = escape_markdown(user.saved_payment_info) if user.saved_payment_info else "لم يتم ضبطه بعد"

    text = (
        f"👤 **الملف الشخصي للمتداول**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• **المعرف:**  {user_display}\n"
        f"• **اللقب:**  `{nickname_display}`\n"
        f"• **رقم الحساب:**  `{user.telegram_id}`\n\n"
        f"📊 **مؤشرات الثقة والتداول:**\n"
        f"• المستوى:  **{tier_badge}**\n"
        f"• نسبة السمعة:  ⭐ **{rep_score}**\n"
        f"• الصفقات المكتملة:  🏁 `{deals_count}` صفقة ناجحة\n"
        f"• إجمالي حجم التداول:  💰 `{format_usdt(total_volume)} USDT`\n"
        f"• حالة التوثيق:  {kyc_display}\n"
        f"• رصيد النقاط الذهبية:  🌟 `{points}` نقطة\n\n"
        f"⚙️ **البيانات المحفوظة تلقائياً:**\n"
        f"• محفظة USDT (BEP20):  `{saved_wallet}`\n"
        f"• حساب الدفع (شام كاش/بنكي):  `{saved_acc}`\n"
        f"━━━━━━━━━━━━━━━\n\n"
        f"اختر أي قسم أدناه للإدارة أو التعديل:"
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
        f"⭐ **سجل السمعة والتقييم الائتماني**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• نسبة التقييم العام:  ⭐ **{rep}%**\n"
        f"• الصفقات المنفذة بنجاح:  🏁 `{deals}` صفقة\n"
        f"• النزاعات المفتوحة:  🛡️ `0` نزاع\n\n"
        f"💡 **كيف يتم احتساب السمعة؟**\n"
        f"• كل صفقة مكتملة بدون نزاع ترفع من رصيدك الائتماني.\n"
        f"• الالتزام بمواعيد الدفع والإطلاق السريع يعزز مكانتك في السوق كتاجر موثوق.\n"
        f"━━━━━━━━━━━━━━━"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 العودة للملف الشخصي", callback_data="menu_user_profile")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

# ================= 2. WALLET ADDRESS MANAGEMENT =================
@profile_router.callback_query(F.data == "prof_wallet")
async def cq_prof_wallet(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    user_id = callback.from_user.id
    res = await db_session.execute(select(User).where(User.telegram_id == user_id))
    user = res.scalar_one_or_none()
    
    current_addr = escape_markdown(user.saved_usdt_address) if user and user.saved_usdt_address else "لم يتم التعيين بعد"

    await state.set_state(ProfileFSM.waiting_for_usdt_address)
    text = (
        f"📍 **إدارة عنوان محفظة USDT (BEP-20 / BNB Smart Chain)**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"• العنوان المحفوظ حالياً:\n`{current_addr}`\n\n"
        f"هذا العنوان سيتم اقتراحه عليك في بوابة تأكيد صفقات الشراء لتوفير الوقت وسرعة الاستلام.\n\n"
        f"⚠️ **تنبيه وقائي هام:**\n"
        f"تأكد أن هذا العنوان من محفظة تدعم شبكة **BNB Smart Chain (BEP20)** تحديداً — إرسال عنوان من شبكة أخرى قد يؤدي لفقدان الأموال بشكل دائم.\n\n"
        f"✍️ **لتعيين أو تغيير العنوان:** أرسل عنوان محفظتك (يبدأ بـ `0x` ومكون من 42 خانة) الآن:"
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
            f"⚠️ **عنوان غير صالح!**\n{err_msg}\n\n"
            "⚠️ **تنبيه وقائي هام:**\n"
            "تأكد أن هذا العنوان من محفظة تدعم شبكة **BNB Smart Chain (BEP20)** تحديداً — إرسال عنوان من شبكة أخرى قد يؤدي لفقدان الأموال بشكل دائم.\n\n"
            "يرجى إعادة المحاولة أو الإلغاء:",
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
        f"✅ **تم حفظ عنوان محفظة USDT (BEP-20) بنجاح!**\n\n`{escape_markdown(normalized_addr)}`",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 العودة للملف الشخصي", callback_data="menu_user_profile")]
        ]),
        parse_mode="Markdown"
    )

# ================= 3. PAYMENT INFO MANAGEMENT =================
@profile_router.callback_query(F.data == "prof_payment")
async def cq_prof_payment(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    user_id = callback.from_user.id
    res = await db_session.execute(select(User).where(User.telegram_id == user_id))
    user = res.scalar_one_or_none()
    
    current_acc = escape_markdown(user.saved_payment_info) if user and user.saved_payment_info else "لم يتم التعيين بعد"

    await state.set_state(ProfileFSM.waiting_for_payment_info)
    text = (
        f"💳 **إدارة حساب استلام الليرة السورية**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"الحساب المحفوظ حالياً:\n`{current_acc}`\n\n"
        f"يتم تزويد المشتري بهذا الحساب تلقائياً عند بيعك لـ USDT (مثال: شام كاش، سيريتل كاش، بيمو، الهرم).\n\n"
        f"✍️ **لتحديث بياناتك:** أرسل تفاصيل الحساب ورقم الهاتف المعتمد الآن:"
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

@profile_router.message(ProfileFSM.waiting_for_payment_info)
async def process_saved_payment_info(message: Message, state: FSMContext, db_session: AsyncSession):
    info = message.text.strip() if message.text else ""
    if len(info) < 5:
        warn_msg = await message.answer(
            "⚠️ يرجى إرسال تفاصيل واضحة لحساب الدفع:",
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
        user.saved_payment_info = info
        await db_session.commit()

    await state.clear()
    await message.answer(
        f"✅ **تم تحديث بيانات حساب الدفع بنجاح!**\n\n`{escape_markdown(info)}`",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 العودة للملف الشخصي", callback_data="menu_user_profile")]
        ]),
        parse_mode="Markdown"
    )

# ================= 4. KYC & VERIFICATION =================
@profile_router.callback_query(F.data == "prof_kyc")
async def cq_prof_kyc(callback: CallbackQuery, db_session: AsyncSession):
    user_id = callback.from_user.id
    res = await db_session.execute(select(User).where(User.telegram_id == user_id))
    user = res.scalar_one_or_none()

    status = user.kyc_status if user else "unverified"
    if status == "verified":
        desc = f"🟢 **حسابك موثّق بالكامل لدى {BOT_NAME}!**\nتتمتع بحدود تداول مفتوحة وشارة التاجر الموثوق."
    elif status == "pending":
        desc = "🟡 **طلب التوثيق قيد المراجعة الإدارية.**\nسيتم إشعارك فور اكتمال التدقيق من قبل فريق الإدارة."
    else:
        desc = (
            "⚪ **حسابك غير موثّق حالياً.**\n\n"
            "مزايا التوثيق (KYC):\n"
            "• شارة التوثيق الذهبية بجانب اسمك في السوق.\n"
            "• أولوية في تداول الصفقات والمبالغ الكبيرة.\n"
            "• زيادة ثقة المشترين والبائعين في عروضك.\n\n"
            "لطلب التوثيق، تواصل مباشرة مع فريق الإدارة عبر زر الدعم الفني."
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📞 التواصل مع الدعم لتوثيق الحساب", callback_data="supp_contact")],
        [InlineKeyboardButton(text="🔙 العودة للملف الشخصي", callback_data="menu_user_profile")]
    ])
    await callback.message.edit_text(
        f"🆔 **توثيق الهوية والحساب (KYC)**\n\n━━━━━━━━━━━━━━━\n{desc}\n━━━━━━━━━━━━━━━",
        reply_markup=kb,
        parse_mode="Markdown"
    )
    await callback.answer()

# ================= 5. REFERRAL SYSTEM =================
@profile_router.callback_query(F.data == "prof_referral")
async def cq_prof_referral(callback: CallbackQuery):
    bot_info = await callback.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{callback.from_user.id}"

    text = (
        f"🔗 **برنامج الإحالة والمكافآت (Referral Program)**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"شارك رابط الإحالة الخاص بك مع أصدقائك وتجار العملات الرقمية:\n\n"
        f"📎 **رابطك الخاص:**\n`{ref_link}`\n\n"
        f"🎁 **المزايا والمكافآت:**\n"
        f"• احصل على نقاط ذهبية عن كل مستخدم جديد يسجل ويبدأ أول صفقة وساطة ناجحة.\n"
        f"• خصومات مباشرة على عمولة الضمان بنسبة تصل إلى 50%.\n"
        f"━━━━━━━━━━━━━━━"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 العودة للملف الشخصي", callback_data="menu_user_profile")]
    ])
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

# ================= 6. GOLDEN POINTS PROGRAM =================
@profile_router.callback_query(F.data == "prof_rewards")
async def cq_prof_rewards(callback: CallbackQuery, db_session: AsyncSession):
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
        "🌟 **ملف نقاطك الشخصي** 🌟\n\n\n"
        f"👤 المستوى الحالي: {level_str}\n"
        f"💎 رصيد نقاطك: {points}\n\n\n"
        "---------------------\n\n\n"
        "🎁 **جوائز استبدال النقاط:**\n\n"
        "🔵 500 نقطة = 5$ رصيد USDT\n"
        "🟡 1500 نقطة = 17$ رصيد USDT\n"
        "🟣 3000 نقطة = 35$ رصيد USDT\n\n"
    )

    buttons = []
    # If user has enough points, show redemption buttons
    if points >= 500:
        buttons.append([InlineKeyboardButton(text="🔵 استبدال 500 نقطة بـ 5$ USDT", callback_data="claim_points_500")])
    if points >= 1500:
        buttons.append([InlineKeyboardButton(text="🟡 استبدال 1500 نقطة بـ 17$ USDT", callback_data="claim_points_1500")])
    if points >= 3000:
        buttons.append([InlineKeyboardButton(text="🟣 استبدال 3000 نقطة بـ 35$ USDT", callback_data="claim_points_3000")])

    buttons.append([InlineKeyboardButton(text="🔙 العودة للملف الشخصي", callback_data="menu_user_profile")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    await callback.answer()

@profile_router.callback_query(F.data.startswith("claim_points_"))
async def cq_claim_points(callback: CallbackQuery, db_session: AsyncSession):
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
        await callback.answer("طلب غير صالح", show_alert=True)
        return

    req_points, reward_usdt = reward_map[action]
    current_points = user.reward_points if user else 0

    if current_points < req_points:
        await callback.answer("⚠️ رصيد نقاطك غير كافٍ للاستبدال!", show_alert=True)
        return

    # Deduct points
    user.reward_points -= req_points
    await db_session.commit()

    # Notify user
    await callback.answer(f"🎉 تهانينا! تم تقديم طلب استبدال الجائزة ({reward_usdt}$ USDT).", show_alert=True)
    
    # Notify Admin for payout
    admin_text = (
        f"🎁 **طلب استبدال نقاط ذهبية جديد**\n\n"
        f"• المستخدم: {safe_username(user.username, user.telegram_id)}\n"
        f"• النقاط المستبدلة: `{req_points}` نقطة\n"
        f"• المكافأة المستحقة: `{reward_usdt}$ USDT`\n"
        f"• محفظة الاستلام: `{escape_markdown(user.saved_usdt_address or 'لم يحدد عنواناً بعد')}`\n"
    )
    for admin_id in settings.ADMIN_IDS:
        if admin_id:
            try:
                await callback.bot.send_message(chat_id=admin_id, text=admin_text, parse_mode="Markdown")
            except Exception:
                pass

    # Refresh screen
    await cq_prof_rewards(callback, db_session)

# ================= 7. SET NICKNAME =================
@profile_router.callback_query(F.data == "prof_set_nickname")
async def cq_prof_set_nickname(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ProfileFSM.waiting_for_nickname)
    text = (
        f"✏️ **تغيير اللقب التجاري**\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"أرسل الآن الاسم أو اللقب التجاري الذي ترغب بظهوره للمتداولين الآخرين في السوق:\n"
        f"*(الحد الأقصى: 30 حرفاً)*"
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
