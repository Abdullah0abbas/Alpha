from datetime import datetime, timezone, timedelta
import logging

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import or_, update

from db.models import Deal, Offer
from bot.services.audit_service import add_audit
from bot.services.channel import update_channel_offer_message
from config.settings import settings
from bot.utils.deal_views import format_admin_deal_review_text
from bot.utils.deal_card import push_deal_card, render_deal_card
from bot.utils.formatters import format_syp, format_usdt
from bot.utils.transitions import can_cancel_deal

logger = logging.getLogger(__name__)

lifecycle_router = Router(name="deals_lifecycle")


# ================= OWNER APPROVAL / REJECTION HANDLERS =================

@lifecycle_router.callback_query(F.data.startswith("appr_deal_"))
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

    await callback.answer("تمت الموافقة على الطلب بنجاح ✅")
    await push_deal_card(deal.id, callback.bot)


@lifecycle_router.callback_query(F.data.startswith("rejc_deal_"))
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
        offer.status_changed_at = datetime.now(timezone.utc)

    add_audit(
        session=db_session,
        action="REJECT_DEAL",
        target_type="deal",
        target_id=deal.id,
        actor_id=callback.from_user.id,
        actor_type="ADMIN" if callback.from_user.id in settings.ADMIN_IDS else "USER",
        details={
            "previous_status": "PENDING_OWNER_APPROVAL",
            "actor_id": callback.from_user.id,
        }
    )

    await db_session.commit()
    if offer:
        await update_channel_offer_message(callback.bot, offer)

    await callback.answer("تم رفض الطلب.")
    await push_deal_card(deal.id, callback.bot)


# ================= DEAL CANCELLATION (EARLY STATES ONLY) =================

@lifecycle_router.callback_query(F.data.startswith("ask_cancel_"))
async def ask_cancel_deal(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()

    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return

    # Fetch offer creator for B restrictions
    offer_res = await db_session.execute(select(Offer).where(Offer.id == deal.offer_id))
    offer = offer_res.scalar_one_or_none()
    offer_owner_id = offer.user_id if offer else -1

    allowed, err_msg = can_cancel_deal(
        deal_status=deal.status,
        user_id=callback.from_user.id,
        seller_id=deal.seller_id,
        buyer_id=deal.buyer_id,
        offer_owner_id=offer_owner_id,
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
        f"▫️ **الكمية:** `{usdt_str} USDT`\n"
        f"▫️ **المبلغ المقابل:** `{syp_str}` ل.س\n\n"
        f"سيتم إلغاء الصفقة وإعادة العرض إلى السوق.\n"
        f"تنبيه: لا يمكن التراجع عن هذا الإجراء بعد تنفيذه. هل ترغب بالمتابعة؟"
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


@lifecycle_router.callback_query(F.data.startswith("back_cancel_deal_"))
async def cq_back_cancel_deal(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(select(Deal).where(Deal.id == deal_id))
    deal = result.scalar_one_or_none()
    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return
    await db_session.commit()

    user_id = callback.from_user.id
    role = "🔴 بائع" if deal.seller_id == user_id else "🟢 مشتري"
    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)

    if deal.status == "PENDING_OWNER_APPROVAL":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء الطلب", callback_data=f"ask_cancel_{deal.id}")]
        ])
        text = (
            f"⏳ **طلب صفقة جديدة #{deal.id}**\n\n"
            f"▫️ **الحالة:** بانتظار موافقة الطرف الآخر\n"
            f"▫️ **دورك في الصفقة:** {role}\n"
            f"▫️ **الكمية:** `{usdt_str} USDT`\n"
            f"▫️ **المبلغ المقابل:** `{syp_str}` ل.س\n"
            f"▫️ **المهلة المتاحة:** 30 دقيقة\n\n"
            f"تم إرسال إشعار لصاحب العرض، وسيصلك تنبيه فور اتخاذ قراره."
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


@lifecycle_router.callback_query(F.data.startswith("cancel_deal_"))
async def cancel_deal(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    result = await db_session.execute(
        select(Deal).where(Deal.id == deal_id).with_for_update()
    )
    deal = result.scalar_one_or_none()

    if not deal:
        await callback.answer("عذراً، لم يتم العثور على الصفقة.", show_alert=True)
        return

    # Fetch offer creator for B restrictions
    offer_result = await db_session.execute(select(Offer).where(Offer.id == deal.offer_id))
    offer = offer_result.scalar_one_or_none()
    offer_owner_id = offer.user_id if offer else -1

    allowed, err_msg = can_cancel_deal(
        deal_status=deal.status,
        user_id=callback.from_user.id,
        seller_id=deal.seller_id,
        buyer_id=deal.buyer_id,
        offer_owner_id=offer_owner_id,
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
        offer.status_changed_at = datetime.now(timezone.utc)

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
        await push_deal_card(deal.id, callback.bot, notify_handoff=False)
    except Exception as e:
        logger.warning(f"Failed to push deal card for cancelled deal #{deal.id}: {e}")

    try:
        await callback.message.edit_text(
            f"❌ **صفقة #{deal.id} — تم إلغاء الصفقة**\n\n"
            f"تم إلغاء الصفقة بنجاح وإعادة العرض إلى السوق.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Failed to edit cancel message for deal #{deal.id}: {e}")
    try:
        await callback.answer()
    except Exception:
        pass


# ================= ACTIVE DEALS LISTING =================

@lifecycle_router.message(Command("continue", "my_deals"))
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

    # 2. Fetch user's own active deals (excluding terminal statuses)
    result = await db_session.execute(
        select(Deal).where(
            or_(Deal.buyer_id == user_id, Deal.seller_id == user_id),
            Deal.status.notin_(["COMPLETED", "CANCELLED", "EXPIRED", "DISPUTED"])
        ).order_by(Deal.created_at.desc())
    )
    deals = result.scalars().all()

    if not deals and not admin_pending_deals:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 الرئيسية", callback_data="menu_back_main")]
        ])
        await message.answer("ℹ️ ليس لديك أي صفقات نشطة حالياً.", reply_markup=kb)
        return

    # If admin, show deals awaiting admin approval first
    if admin_pending_deals:
        await message.answer("🛡️ **صفقات بانتظار الاعتماد الإداري:**", parse_mode="Markdown")
        for deal in admin_pending_deals:
            admin_text = await format_admin_deal_review_text(deal, db_session)
            admin_kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="☑️ اعتماد وإطلاق الضمان", callback_data=f"ask_rel_{deal.id}")]
            ])
            await message.answer(admin_text, reply_markup=admin_kb, parse_mode="Markdown")

    # Show user's personal deals using unified render_deal_card
    if deals:
        if admin_pending_deals:
            await message.answer("📋 **صفقاتك النشطة:**", parse_mode="Markdown")

        for deal in deals:
            old_msg_id = deal.buyer_card_msg_id if deal.buyer_id == user_id else deal.seller_card_msg_id
            is_seller_proof_photo = (deal.seller_id == user_id and deal.status == "SYP_PAYMENT_SENT")
            if old_msg_id and not is_seller_proof_photo and hasattr(message, "bot") and message.bot:
                try:
                    await message.bot.delete_message(chat_id=user_id, message_id=old_msg_id)
                except Exception:
                    pass

            offer_res = await db_session.execute(select(Offer).where(Offer.id == deal.offer_id))
            offer = offer_res.scalar_one_or_none()
            is_offer_owner = bool(offer and offer.user_id == user_id)
            card_text, kb = render_deal_card(deal, user_id, is_offer_owner=is_offer_owner)
            sent_msg = await message.answer(card_text, reply_markup=kb, parse_mode="Markdown")

            if not is_seller_proof_photo:
                col = Deal.buyer_card_msg_id if deal.buyer_id == user_id else Deal.seller_card_msg_id
                await db_session.execute(
                    update(Deal).where(Deal.id == deal.id).values({col: sent_msg.message_id})
                )
        await db_session.commit()
