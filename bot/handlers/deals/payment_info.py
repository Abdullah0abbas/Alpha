from datetime import datetime, timezone, timedelta
from typing import Union
import logging

from aiogram import Router, F, Bot
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select
from sqlalchemy import update

from db.models import Deal, Offer, User
from bot.states.flow_states import DealPaymentFSM
from config.settings import settings
from bot.utils.deal_card import push_deal_card
from bot.utils.deal_guard import acquire_deal_lock
from bot.utils.validators import validate_bep20_address, validate_payment_method_info
from bot.utils.payment_prompts import (
    build_allowed_pm_selection_kb,
    build_deal_pm_prompt,
    extract_clean_account,
    match_saved_payment_info,
    resolve_pm_key,
)
from bot.utils.formatters import escape_markdown, format_syp, format_usdt
from bot.utils.transitions import SELLER_INFO_ALLOWED_STATUSES

logger = logging.getLogger(__name__)

payment_info_router = Router(name="deals_payment_info")


# ================= UNIFIED PAYMENT & WALLET HELPERS =================

async def apply_seller_payment_info(
    deal: Deal,
    info_text: str,
    bot: Bot,
    db_session: AsyncSession,
    event: Union[CallbackQuery, Message],
    selected_pm: str | None = None
) -> None:
    if deal.status != "USDT_DEPOSITED":
        raise ValueError(f"Invalid deal status for seller payment info: {deal.status}")

    existing_method = deal.seller_payment_info.get("method") if isinstance(deal.seller_payment_info, dict) else None
    method_val = selected_pm or existing_method or "غير محدد"
    clean_info = extract_clean_account(info_text, method_val) or info_text

    deal.seller_payment_info = {"method": method_val, "info": clean_info}
    deal.status = "AWAITING_SYP_PAYMENT"
    deal.expires_at = datetime.now(timezone.utc) + timedelta(minutes=30)
    await db_session.commit()

    if isinstance(event, CallbackQuery):
        try:
            await event.answer("تم اعتماد بيانات الحساب بنجاح ✅")
        except Exception:
            pass
    elif isinstance(event, Message):
        try:
            await event.answer("✅ تم اعتماد بيانات الحساب بنجاح.")
        except Exception:
            pass

    await push_deal_card(deal.id, bot)


async def forward_proof_photo_to_seller(deal: Deal, photo_file_id: str, bot: Bot, db_session: AsyncSession | None = None):
    # 1. Clean up any previous card message sent to the seller so there is strictly ONE message
    if deal.seller_card_msg_id:
        try:
            await bot.delete_message(chat_id=deal.seller_id, message_id=deal.seller_card_msg_id)
        except Exception:
            pass

    syp_str = format_syp(deal.amount_syp)
    usdt_str = format_usdt(deal.amount_usdt)

    seller_info_val = ""
    seller_method = ""
    if isinstance(deal.seller_payment_info, dict):
        seller_method = str(deal.seller_payment_info.get("method") or "")
        seller_info_val = str(deal.seller_payment_info.get("info") or "")
    elif isinstance(deal.seller_payment_info, str):
        seller_info_val = deal.seller_payment_info

    payment_info_str = (
        f"{seller_method}\n`{escape_markdown(seller_info_val)}`"
        if (seller_method and seller_method not in ["غير محدد", "PENDING"])
        else f"`{escape_markdown(seller_info_val)}`"
    )

    seller_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ تأكيد الاستلام", callback_data=f"ask_conf_{deal.id}"),
            InlineKeyboardButton(text="⚠️ لم أستلم", callback_data=f"dispute_deal_{deal.id}")
        ]
    ])
    caption = (
        f"📸 **إشعار تحويل الليرة — صفقة #{deal.id}**\n"
        "〰️〰️〰️〰️〰️〰️〰️\n"
        f"👤 **دورك :** 🔴 بائع\n"
        f"💴 **المبلغ المطلوب استلامه :** `{syp_str}` ل.س\n"
        f"💰 **رصيد الضمان :** `{usdt_str} USDT`\n"
        f"💳 **حساب الاستلام :**\n{payment_info_str}\n\n"
        f"⚠️ تحقق من وصول المبلغ فعلياً إلى حسابك قبل الضغط على **تأكيد الاستلام**."
    )
    try:
        sent_msg = await bot.send_photo(
            chat_id=deal.seller_id,
            photo=photo_file_id,
            caption=caption,
            reply_markup=seller_kb,
            parse_mode="Markdown"
        )
        deal.seller_card_msg_id = sent_msg.message_id
        if db_session:
            await db_session.execute(
                update(Deal).where(Deal.id == deal.id).values(seller_card_msg_id=sent_msg.message_id)
            )
            await db_session.commit()
    except Exception as e:
        logger.warning(f"Failed to send proof photo to seller {deal.seller_id}: {e}")


async def apply_buyer_payout_wallet(
    deal: Deal,
    wallet_address: str,
    bot: Bot,
    db_session: AsyncSession,
    event: Union[CallbackQuery, Message],
    photo_file_id: str | None = None
) -> None:
    if deal.status not in ("AWAITING_SYP_PAYMENT", "SYP_PAYMENT_SENT", "ADMIN_APPROVAL"):
        raise ValueError(f"Invalid deal status for buyer payout wallet: {deal.status}")

    is_valid, normalized_address, err_msg = validate_bep20_address(wallet_address)
    if not is_valid:
        raise ValueError(f"Invalid BEP-20 wallet address: {err_msg}")

    deal.buyer_payout_address = normalized_address

    if deal.status == "AWAITING_SYP_PAYMENT":
        deal.status = "SYP_PAYMENT_SENT"
        deal.expires_at = None
        await db_session.commit()

        if isinstance(event, CallbackQuery):
            try:
                await event.answer("✅ تم إرسال الإشعار بنجاح.")
            except Exception:
                pass
        elif isinstance(event, Message):
            try:
                await event.answer("✅ تم إرسال البيانات بانتظار التحقق.")
            except Exception:
                pass

        # Push deal card ONLY to buyer to avoid sending duplicate text card to seller:
        await push_deal_card(deal.id, bot, specific_user_id=deal.buyer_id)

        if photo_file_id:
            await forward_proof_photo_to_seller(deal, photo_file_id, bot, db_session)

    elif deal.status == "SYP_PAYMENT_SENT":
        await db_session.commit()

        escaped_wallet = escape_markdown(normalized_address)
        buyer_text = (
            f"📝 **تأكيد معلومات USDT (تم تسجيل عنوان محفظة الاستلام):**\n"
            f"`{escaped_wallet}`\n\n"
            f"تم إرسال البيانات بانتظار التحقق."
        )

        if isinstance(event, CallbackQuery):
            try:
                await event.message.edit_text(buyer_text, parse_mode="Markdown")
            except Exception:
                await event.message.answer(buyer_text, parse_mode="Markdown")
            try:
                await event.answer("تم تسجيل عنوان المحفظة ✅")
            except Exception:
                pass
        else:
            await event.answer(buyer_text, parse_mode="Markdown")

        # Refresh buyer's card to remove fallback wallet button without touching seller's proof photo
        await push_deal_card(deal.id, bot, notify_handoff=False, specific_user_id=deal.buyer_id)

    elif deal.status == "ADMIN_APPROVAL":
        await db_session.commit()

        escaped_wallet = escape_markdown(normalized_address)
        buyer_text = (
            f"📝 **تأكيد معلومات USDT (تم تسجيل عنوان محفظة الاستلام):**\n"
            f"`{escaped_wallet}`\n\n"
            f"تم إرسال البيانات بانتظار إرسال الـ USDT من الإدارة."
        )

        if isinstance(event, CallbackQuery):
            try:
                await event.message.edit_text(buyer_text, parse_mode="Markdown")
            except Exception:
                await event.message.answer(buyer_text, parse_mode="Markdown")
            await event.answer()
        else:
            await event.answer(buyer_text, parse_mode="Markdown")

        # Notify Admins that payout address has been registered
        admin_notice = (
            f"ℹ️ **تحديث صفقة #{deal.id}**\n"
            f"💵 **عنوان USDT (BEP-20) للمشتري:**\n"
            f"`{escaped_wallet}`"
        )
        for admin_id in settings.ADMIN_IDS:
            try:
                await bot.send_message(
                    chat_id=admin_id,
                    text=admin_notice,
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.warning(f"Failed to notify admin {admin_id} for deal #{deal.id}: {e}")


# ================= SELLER PAYMENT ACCOUNT HANDLERS =================

async def get_deal_locked_pm_and_offer_methods(
    deal: Deal,
    db_session: AsyncSession,
) -> tuple[str | None, list[str] | None]:
    """
    Returns (locked_method_name, offer_allowed_methods).
    - If deal already specifies a concrete payment method, it is locked to that method.
    - Otherwise fetches offer.payment_methods; if exactly 1 method exists, locks to it;
      if multiple exist, returns (None, offer_methods) so the seller chooses from those only.
    """
    if isinstance(deal.seller_payment_info, dict):
        existing = deal.seller_payment_info.get("method")
        if existing and existing not in ("غير محدد", "PENDING"):
            return str(existing), None

    offer_res = await db_session.execute(select(Offer).where(Offer.id == deal.offer_id))
    offer_obj = offer_res.scalar_one_or_none()
    offer_methods = list(offer_obj.payment_methods) if (offer_obj and offer_obj.payment_methods) else None
    if offer_methods and len(offer_methods) == 1:
        return str(offer_methods[0]), offer_methods
    return None, offer_methods


async def prompt_seller_payment_info_flow(
    deal: Deal,
    callback: CallbackQuery,
    state: FSMContext,
    db_session: AsyncSession,
    header_prefix: str = "",
    commit_before_send: bool = True,
) -> None:
    """
    Shared Seller SYP payout prompt:
    1. Checks profile saved_payment_info compatibility against locked deal method or offer's allowed methods.
    2. If compatible -> shows Confirmation Gate.
    3. If incompatible or missing -> prompts directly for locked method OR shows selection restricted to offer methods.
    """
    locked_pm, offer_methods = await get_deal_locked_pm_and_offer_methods(deal, db_session)

    user_res = await db_session.execute(select(User).where(User.telegram_id == deal.seller_id))
    seller_user = user_res.scalar_one_or_none()
    saved_info = seller_user.saved_payment_info.strip() if (seller_user and seller_user.saved_payment_info) else None

    is_saved_compatible, saved_method, clean_saved_acc, incompatible_reason = match_saved_payment_info(
        saved_info,
        target_method=locked_pm,
        allowed_methods=offer_methods,
    )

    if commit_before_send:
        await db_session.commit()

    if is_saved_compatible and saved_method and clean_saved_acc:
        gate_text = (
            f"{header_prefix}"
            f"💳 **حساب استلام الليرة السورية — صفقة #{deal.id}**\n\n"
            f"▫️ **الوسيلة المحفوظة:** {saved_method}\n"
            f"▫️ **البيانات:**\n"
            f"`{escape_markdown(clean_saved_acc)}`\n\n"
            f"هل ترغب في استخدام هذه البيانات المحفوظة، أم إدخال بيانات جديدة لهذه الصفقة؟"
        )
        gate_kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ استخدام هذه البيانات", callback_data=f"use_saved_seller_info_{deal.id}")],
            [InlineKeyboardButton(text="✏️ إدخال بيانات جديدة لهذه الصفقة", callback_data=f"manual_set_seller_info_{deal.id}")],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")],
        ])
        try:
            await callback.message.edit_text(gate_text, reply_markup=gate_kb, parse_mode="Markdown")
        except Exception:
            await callback.message.answer(gate_text, reply_markup=gate_kb, parse_mode="Markdown")
        await callback.answer()
        return

    incompat_note = ""
    if saved_info and incompatible_reason:
        incompat_note = f"ℹ️ _{incompatible_reason}_\n\n"

    if locked_pm:
        _, prompt_text = build_deal_pm_prompt(locked_pm, deal.id)
        full_text = f"{header_prefix}{incompat_note}{prompt_text}"
        await state.set_state(DealPaymentFSM.waiting_for_seller_account)
        await state.update_data(deal_id=deal.id, selected_pm=locked_pm)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
        ])
        try:
            sent_msg = await callback.message.edit_text(full_text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            sent_msg = await callback.message.answer(full_text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
        await callback.answer()
        return

    curr_info = deal.seller_payment_info.get("info", "") if isinstance(deal.seller_payment_info, dict) else ""
    curr_info_str = f"\n▫️ **البيانات المسجلة حالياً:**\n`{escape_markdown(curr_info)}`\n" if curr_info and curr_info not in ["PENDING", "غير محدد", "N/A"] else ""
    select_text = (
        f"{header_prefix}{incompat_note}"
        f"💳 **حساب استلام الليرة السورية — صفقة #{deal.id}**\n"
        f"{curr_info_str}\n"
        f"اختر وسيلة الدفع التي ترغب باستلام الدفعة عبرها:"
    )
    select_kb = build_allowed_pm_selection_kb(
        allowed_methods=offer_methods,
        callback_prefix="deal_pm_",
        back_text="❌ إلغاء",
        back_callback="fsm_cancel_back",
        callback_suffix=f"_{deal.id}",
    )
    try:
        await callback.message.edit_text(select_text, reply_markup=select_kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(select_text, reply_markup=select_kb, parse_mode="Markdown")
    await callback.answer()


@payment_info_router.callback_query(F.data.startswith("deal_pm_"))
async def cq_deal_choose_payment_method(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    parts = callback.data.split("_")
    deal_id = int(parts[-1])
    method_key = "_".join(parts[2:-1])

    prompt_res = build_deal_pm_prompt(method_key, deal_id, strict=True)
    if not prompt_res:
        await callback.answer("طريقة دفع غير معروفة.", show_alert=True)
        return
    pm_name, prompt_text = prompt_res

    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses=SELLER_INFO_ALLOWED_STATUSES,
        allowed_roles="seller",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    locked_pm, offer_methods = await get_deal_locked_pm_and_offer_methods(deal, db_session)
    if locked_pm and resolve_pm_key(locked_pm) != resolve_pm_key(pm_name):
        await db_session.commit()
        await callback.answer(f"⚠️ هذه الصفقة مقيدة بوسيلة الدفع ({locked_pm}) فقط.", show_alert=True)
        return
    if not locked_pm and offer_methods:
        allowed_keys = {resolve_pm_key(m) for m in offer_methods}
        if resolve_pm_key(pm_name) not in allowed_keys:
            await db_session.commit()
            await callback.answer("⚠️ هذه الوسيلة غير مدعومة في هذا العرض.", show_alert=True)
            return

    await db_session.commit()

    await state.set_state(DealPaymentFSM.waiting_for_seller_account)
    await state.update_data(deal_id=deal_id, selected_pm=pm_name)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    try:
        sent_msg = await callback.message.edit_text(prompt_text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    except Exception:
        sent_msg = await callback.message.answer(prompt_text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    await callback.answer()


@payment_info_router.callback_query(F.data.startswith("set_seller_info_"))
async def start_set_seller_info(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses=SELLER_INFO_ALLOWED_STATUSES,
        allowed_roles="seller",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    await prompt_seller_payment_info_flow(
        deal=deal,
        callback=callback,
        state=state,
        db_session=db_session,
        commit_before_send=True,
    )


@payment_info_router.callback_query(F.data.startswith("use_saved_seller_info_"))
async def cq_use_saved_seller_info(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses=SELLER_INFO_ALLOWED_STATUSES,
        allowed_roles="seller",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    locked_pm, offer_methods = await get_deal_locked_pm_and_offer_methods(deal, db_session)

    user_res = await db_session.execute(select(User).where(User.telegram_id == deal.seller_id))
    seller_user = user_res.scalar_one_or_none()
    saved_info = seller_user.saved_payment_info.strip() if (seller_user and seller_user.saved_payment_info) else None

    if not saved_info:
        await db_session.commit()
        await callback.answer("لم يتم العثور على بيانات محفوظة.", show_alert=True)
        return

    is_usable, resolved_method, clean_acc, err_reason = match_saved_payment_info(
        saved_info,
        target_method=locked_pm,
        allowed_methods=offer_methods,
    )
    if not is_usable or not resolved_method or not clean_acc:
        await db_session.commit()
        await callback.answer(f"⚠️ {err_reason}", show_alert=True)
        return

    await apply_seller_payment_info(
        deal=deal,
        info_text=clean_acc,
        bot=callback.bot,
        db_session=db_session,
        event=callback,
        selected_pm=resolved_method,
    )


@payment_info_router.callback_query(F.data.startswith("manual_set_seller_info_"))
async def cq_manual_set_seller_info(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses=SELLER_INFO_ALLOWED_STATUSES,
        allowed_roles="seller",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    locked_pm, offer_methods = await get_deal_locked_pm_and_offer_methods(deal, db_session)
    await db_session.commit()

    if locked_pm:
        _, prompt_text = build_deal_pm_prompt(locked_pm, deal.id)
        await state.set_state(DealPaymentFSM.waiting_for_seller_account)
        await state.update_data(deal_id=deal.id, selected_pm=locked_pm)

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
        ])
        try:
            sent_msg = await callback.message.edit_text(prompt_text, reply_markup=kb, parse_mode="Markdown")
        except Exception:
            sent_msg = await callback.message.answer(prompt_text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
        await callback.answer()
        return

    select_kb = build_allowed_pm_selection_kb(
        allowed_methods=offer_methods,
        callback_prefix="deal_pm_",
        back_text="❌ إلغاء",
        back_callback="fsm_cancel_back",
        callback_suffix=f"_{deal.id}",
    )
    select_text = (
        f"💳 **حساب استلام الليرة السورية — صفقة #{deal.id}**\n\n"
        f"اختر وسيلة الدفع التي ترغب باستلام الدفعة عبرها:"
    )
    try:
        await callback.message.edit_text(select_text, reply_markup=select_kb, parse_mode="Markdown")
    except Exception:
        await callback.message.answer(select_text, reply_markup=select_kb, parse_mode="Markdown")
    await callback.answer()


@payment_info_router.message(DealPaymentFSM.waiting_for_seller_account)
async def process_set_seller_info(message: Message, state: FSMContext, db_session: AsyncSession):
    info_text = message.text.strip() if message.text else ""
    if not info_text:
        await message.answer("يرجى إدخال بيانات صحيحة وغير فارغة:")
        return

    data = await state.get_data()
    deal_id = data.get('deal_id')
    selected_pm = data.get('selected_pm')
    if not deal_id:
        await message.answer("حدث خطأ في استعادة بيانات الجلسة.")
        await state.clear()
        return

    is_valid, formatted_info, err_msg = validate_payment_method_info(selected_pm, info_text)
    if not is_valid:
        warn_msg = await message.answer(
            f"{err_msg}\n\nيرجى إعادة إدخال البيانات بالشكل المطلوب أو الضغط على إلغاء:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
            ]),
            parse_mode="Markdown"
        )
        await state.update_data(prompt_msg_id=warn_msg.message_id, prompt_chat_id=warn_msg.chat.id)
        return

    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=message.from_user.id,
        expected_statuses=SELLER_INFO_ALLOWED_STATUSES,
        allowed_roles="seller",
        db_session=db_session
    )
    if not valid:
        await db_session.commit()
        await message.answer(err_msg)
        await state.clear()
        return

    clean_account = extract_clean_account(formatted_info, selected_pm) or formatted_info
    await state.clear()
    await apply_seller_payment_info(
        deal=deal,
        info_text=clean_account,
        bot=message.bot,
        db_session=db_session,
        event=message,
        selected_pm=selected_pm
    )


# ================= BUYER PAYOUT WALLET HANDLERS =================

@payment_info_router.callback_query(F.data.startswith("set_buyer_payout_"))
async def start_set_buyer_payout(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses=["AWAITING_SYP_PAYMENT", "SYP_PAYMENT_SENT", "ADMIN_APPROVAL"],
        allowed_roles="buyer",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    if deal.status in ("SYP_PAYMENT_SENT", "ADMIN_APPROVAL"):
        curr_addr = (deal.buyer_payout_address or "").strip()
        if curr_addr and curr_addr not in ["PENDING", "غير محدد", "N/A"]:
            await db_session.commit()
            await callback.answer(
                "عنوان المحفظة مسجل مسبقاً ولا يمكن تغييره بعد إرسال إشعار الدفع.",
                show_alert=True
            )
            return

    # Section 16 (item 2.1): Check if buyer has saved BEP-20 wallet address in profile
    user_res = await db_session.execute(select(User).where(User.telegram_id == deal.buyer_id))
    buyer_user = user_res.scalar_one_or_none()
    saved_wallet = buyer_user.saved_usdt_address.strip() if (buyer_user and buyer_user.saved_usdt_address) else None

    is_saved_valid, normalized_saved, _ = validate_bep20_address(saved_wallet) if saved_wallet else (False, "", "")

    # Release row lock before Telegram calls without expiring attributes
    await db_session.commit()

    if is_saved_valid:
        # Show explicit Confirmation Gate
        gate_text = (
            f"📝 **تأكيد معلومات USDT (BEP-20) — صفقة #{deal.id}:**\n"
            f"`{escape_markdown(normalized_saved)}`"
        )
        gate_kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ اعتماد", callback_data=f"use_saved_buyer_wallet_{deal.id}"),
                InlineKeyboardButton(text="✏️ تعديل", callback_data=f"manual_set_buyer_wallet_{deal.id}")
            ],
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
        ])
        try:
            await callback.message.edit_text(gate_text, reply_markup=gate_kb, parse_mode="Markdown")
        except Exception:
            await callback.message.answer(gate_text, reply_markup=gate_kb, parse_mode="Markdown")
        await callback.answer()
        return

    # Fallback to direct input if no saved wallet
    await state.set_state(DealPaymentFSM.waiting_for_buyer_wallet)
    await state.update_data(deal_id=deal_id)

    curr_addr = deal.buyer_payout_address or ""
    curr_addr_str = f"\nالعنوان الحالي: `{escape_markdown(curr_addr)}`\n" if curr_addr and curr_addr not in ["PENDING", "غير محدد", "N/A"] else ""

    await callback.message.answer(
        f"💵 أرسل عنوان USDT (BEP-20) فقط، من دون أي نص أو كلمات إضافية.{curr_addr_str}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
        ]),
        parse_mode="Markdown"
    )
    await callback.answer()


@payment_info_router.callback_query(F.data.startswith("use_saved_buyer_wallet_"))
async def cq_use_saved_buyer_wallet(callback: CallbackQuery, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses=["AWAITING_SYP_PAYMENT", "SYP_PAYMENT_SENT", "ADMIN_APPROVAL"],
        allowed_roles="buyer",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    if deal.status in ("SYP_PAYMENT_SENT", "ADMIN_APPROVAL"):
        curr_addr = (deal.buyer_payout_address or "").strip()
        if curr_addr and curr_addr not in ["PENDING", "غير محدد", "N/A"]:
            await db_session.commit()
            await callback.answer(
                "عنوان المحفظة مسجل مسبقاً ولا يمكن تغييره بعد إرسال إشعار الدفع.",
                show_alert=True
            )
            return

    user_res = await db_session.execute(select(User).where(User.telegram_id == deal.buyer_id))
    buyer_user = user_res.scalar_one_or_none()
    saved_wallet = buyer_user.saved_usdt_address.strip() if (buyer_user and buyer_user.saved_usdt_address) else None

    if not saved_wallet:
        await callback.answer("لم يتم العثور على عنوان محفوظ.", show_alert=True)
        return

    is_valid, normalized_wallet, err_msg = validate_bep20_address(saved_wallet)
    if not is_valid:
        await callback.answer(f"⚠️ عنوان المحفظة المحفوظ غير صالح: {err_msg}", show_alert=True)
        return

    await apply_buyer_payout_wallet(
        deal=deal,
        wallet_address=normalized_wallet,
        bot=callback.bot,
        db_session=db_session,
        event=callback
    )


@payment_info_router.callback_query(F.data.startswith("manual_set_buyer_wallet_"))
async def cq_manual_set_buyer_wallet(callback: CallbackQuery, state: FSMContext, db_session: AsyncSession):
    deal_id = int(callback.data.split("_")[-1])
    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=callback.from_user.id,
        expected_statuses=["AWAITING_SYP_PAYMENT", "SYP_PAYMENT_SENT", "ADMIN_APPROVAL"],
        allowed_roles="buyer",
        db_session=db_session
    )
    if not valid:
        await callback.answer(err_msg, show_alert=True)
        return

    if deal.status in ("SYP_PAYMENT_SENT", "ADMIN_APPROVAL"):
        curr_addr = (deal.buyer_payout_address or "").strip()
        if curr_addr and curr_addr not in ["PENDING", "غير محدد", "N/A"]:
            await db_session.commit()
            await callback.answer(
                "عنوان المحفظة مسجل مسبقاً ولا يمكن تغييره بعد إرسال إشعار الدفع.",
                show_alert=True
            )
            return

    await state.set_state(DealPaymentFSM.waiting_for_buyer_wallet)
    await state.update_data(deal_id=deal_id)

    # Release row lock before Telegram calls without expiring attributes
    await db_session.commit()

    text = "💵 أرسل عنوان USDT (BEP-20) فقط، من دون أي نص أو كلمات إضافية."
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
    ])
    try:
        sent_msg = await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    except Exception:
        sent_msg = await callback.message.answer(text, reply_markup=kb, parse_mode="Markdown")
        await state.update_data(prompt_msg_id=sent_msg.message_id, prompt_chat_id=sent_msg.chat.id)
    await callback.answer()


@payment_info_router.message(DealPaymentFSM.waiting_for_buyer_wallet)
async def process_set_buyer_wallet(message: Message, state: FSMContext, db_session: AsyncSession):
    wallet_raw = message.text if message.text else ""
    is_valid, normalized_address, val_err = validate_bep20_address(wallet_raw)
    if not is_valid:
        warn_msg = await message.answer(
            f"⚠️ **عنوان محفظة غير صالح**\n\n"
            f"{val_err}\n\n"
            f"💵 أرسل عنوان USDT (BEP-20) يبدأ بـ `0x` (42 خانة):",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ إلغاء", callback_data="fsm_cancel_back")]
            ]),
            parse_mode="Markdown"
        )
        await state.update_data(prompt_msg_id=warn_msg.message_id, prompt_chat_id=warn_msg.chat.id)
        return

    data = await state.get_data()
    deal_id = data.get('deal_id')
    if not deal_id:
        await message.answer("حدث خطأ في استعادة بيانات الجلسة.")
        await state.clear()
        return

    valid, deal, err_code, err_msg = await acquire_deal_lock(
        deal_id=deal_id,
        user_id=message.from_user.id,
        expected_statuses=["AWAITING_SYP_PAYMENT", "SYP_PAYMENT_SENT", "ADMIN_APPROVAL"],
        allowed_roles="buyer",
        db_session=db_session
    )
    if not valid:
        await db_session.commit()
        await message.answer(err_msg)
        await state.clear()
        return

    if deal.status in ("SYP_PAYMENT_SENT", "ADMIN_APPROVAL"):
        curr_addr = (deal.buyer_payout_address or "").strip()
        if curr_addr and curr_addr not in ["PENDING", "غير محدد", "N/A"]:
            await db_session.commit()
            await message.answer(
                "عنوان المحفظة مسجل مسبقاً ولا يمكن تغييره بعد إرسال إشعار الدفع."
            )
            await state.clear()
            return

    await state.clear()
    await apply_buyer_payout_wallet(
        deal=deal,
        wallet_address=normalized_address,
        bot=message.bot,
        db_session=db_session,
        event=message
    )
