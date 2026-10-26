"""
Unified Deal Card module for Alpha Bot.
Provides pure rendering, step & turn tracking, keyboard generation, and
robust Telegram in-place update orchestration (push_deal_card).
"""
import logging
from decimal import Decimal
from datetime import datetime, timezone
from typing import Tuple, Optional

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError

from sqlalchemy import update
from sqlalchemy.future import select
from db.session import AsyncSessionLocal
from db.models import Deal, Offer

from config.settings import settings
from bot.utils.formatters import (
    format_usdt,
    format_syp,
    humanize_time_remaining,
    escape_markdown,
    PAYMENT_METHOD_ICONS,
)

logger = logging.getLogger(__name__)


def step_of(status: str) -> Tuple[int, str, str]:
    """
    Maps deal status to (step_number, progress_bar, step_name_ar).
    7 primary steps + terminal statuses (0).
    """
    if status == "PENDING_OWNER_APPROVAL":
        return 1, "[ 🟩 ⬜ ⬜ ⬜ ⬜ ⬜ ⬜ ]", "موافقة صاحب العرض"
    elif status in ("CREATED", "AWAITING_USDT_DEPOSIT"):
        return 2, "[ 🟩 🟩 ⬜ ⬜ ⬜ ⬜ ⬜ ]", "إيداع رصيد الضمان"
    elif status == "USDT_DEPOSITED":
        return 3, "[ 🟩 🟩 🟩 ⬜ ⬜ ⬜ ⬜ ]", "بيانات استلام الليرة"
    elif status == "AWAITING_SYP_PAYMENT":
        return 4, "[ 🟩 🟩 🟩 🟩 ⬜ ⬜ ⬜ ]", "تحويل الليرة السورية"
    elif status == "SYP_PAYMENT_SENT":
        return 5, "[ 🟩 🟩 🟩 🟩 🟩 ⬜ ⬜ ]", "تأكيد استلام الليرة"
    elif status in ("SYP_PAYMENT_CONFIRMED", "ADMIN_APPROVAL"):
        return 6, "[ 🟩 🟩 🟩 🟩 🟩 🟩 ⬜ ]", "مراجعة واعتماد الإدارة"
    elif status in ("RELEASING", "COMPLETED"):
        return 7, "[ 🟩 🟩 🟩 🟩 🟩 🟩 🟩 ]", "إطلاق الرصيد والاكتمال"
    elif status == "CANCELLED":
        return 0, "[ ❌ ❌ ❌ ❌ ❌ ❌ ❌ ]", "ملغاة"
    elif status == "EXPIRED":
        return 0, "[ ⌛ ⌛ ⌛ ⌛ ⌛ ⌛ ⌛ ]", "منتهية الصلاحية"
    elif status == "DISPUTED":
        return 0, "[ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ]", "نزاع مفتوح"
    return 0, "[ ⬜ ⬜ ⬜ ⬜ ⬜ ⬜ ⬜ ]", status


def turn_of(status: str, viewer_role: str, is_offer_owner: bool = False) -> Tuple[bool, str]:
    """
    Calculates if it is currently viewer's turn, along with an Arabic status description.
    viewer_role: 'seller' | 'buyer' | 'admin'
    """
    if status == "PENDING_OWNER_APPROVAL":
        if is_offer_owner:
            return True, "الموافقة على طلب الصفقة أو رفضه"
        return False, "بانتظار موافقة صاحب العرض"

    if status in ("CREATED", "AWAITING_USDT_DEPOSIT"):
        if viewer_role == "seller":
            return True, "تحويل رصيد الضمان إلى المحفظة المخصصة"
        return False, "بانتظار قيام البائع بإيداع رصيد الضمان"

    if status == "USDT_DEPOSITED":
        if viewer_role == "seller":
            return True, "تحديد وسيلة وحساب استلام الليرة السورية"
        return False, "بانتظار تزويد البائع لبيانات حسابه"

    if status == "AWAITING_SYP_PAYMENT":
        if viewer_role == "buyer":
            return True, "تحويل المبلغ لحساب البائع ثم تأكيد التحويل"
        return False, "بانتظار قيام المشتري بتحويل المبلغ بالليرة"

    if status == "SYP_PAYMENT_SENT":
        if viewer_role == "seller":
            return True, "التحقق من حسابك وتأكيد استلام الليرة"
        return False, "بانتظار مراجعة البائع وتأكيده لاستلام المبلغ"

    if status in ("SYP_PAYMENT_CONFIRMED", "ADMIN_APPROVAL"):
        if viewer_role == "admin":
            return True, "مراجعة الصفقة واعتماد إطلاق الضمان"
        return False, "بانتظار تدقيق واعتماد الإدارة لإطلاق الضمان"

    if status in ("RELEASING", "COMPLETED"):
        return False, "اكتملت الصفقة بنجاح وتم إطلاق الرصيد"

    if status == "CANCELLED":
        return False, "تم إلغاء الصفقة"

    if status == "EXPIRED":
        return False, "انتهت مهلة الصفقة تلقائياً"

    if status == "DISPUTED":
        return False, "الصفقة قيد فض النزاع من الإدارة"

    return False, "الصفقة قيد المتابعة"


def get_deal_action_keyboard(
    deal: Deal,
    viewer_id: int,
    is_offer_owner: bool = False
) -> Optional[InlineKeyboardMarkup]:
    """
    Centralized, pure keyboard builder for both Deal Card and /my_deals.
    Ensures a single source of truth for deal interaction buttons across the app.
    Routes dangerous / critical actions through ask_* confirmation screens.
    """
    buttons = []
    status = deal.status
    is_seller = (deal.seller_id == viewer_id)
    is_buyer = (deal.buyer_id == viewer_id)
    is_admin = (viewer_id in settings.ADMIN_IDS)

    if status == "PENDING_OWNER_APPROVAL":
        if is_offer_owner:
            buttons.append([
                InlineKeyboardButton(text="✅ موافقة", callback_data=f"appr_deal_{deal.id}"),
                InlineKeyboardButton(text="❌ رفض", callback_data=f"rejc_deal_{deal.id}")
            ])
        else:
            # Taker can cancel before acceptance
            buttons.append([
                InlineKeyboardButton(text="❌ إلغاء الطلب", callback_data=f"ask_cancel_{deal.id}")
            ])

    elif status == "CREATED":
        if is_seller:
            buttons.append([
                InlineKeyboardButton(text="🔐 بدء إيداع الضمان", callback_data=f"man_aw_dep_{deal.id}")
            ])
            buttons.append([
                InlineKeyboardButton(text="❌ إلغاء الصفقة", callback_data=f"ask_cancel_{deal.id}")
            ])
        elif is_buyer:
            buttons.append([
                InlineKeyboardButton(text="❌ إلغاء الصفقة", callback_data=f"ask_cancel_{deal.id}")
            ])

    elif status == "AWAITING_USDT_DEPOSIT":
        if is_seller:
            buttons.append([
                InlineKeyboardButton(text="✅ تأكيد إيداع الضمان", callback_data=f"man_dep_{deal.id}")
            ])
            buttons.append([
                InlineKeyboardButton(text="❌ إلغاء الصفقة", callback_data=f"ask_cancel_{deal.id}")
            ])
        elif is_buyer:
            buttons.append([
                InlineKeyboardButton(text="❌ إلغاء الصفقة", callback_data=f"ask_cancel_{deal.id}")
            ])

    elif status == "USDT_DEPOSITED":
        if is_seller:
            seller_info_val = ""
            if isinstance(deal.seller_payment_info, dict):
                seller_info_val = str(deal.seller_payment_info.get("info") or "").strip()
            elif isinstance(deal.seller_payment_info, str):
                seller_info_val = deal.seller_payment_info.strip()

            if not seller_info_val or seller_info_val in ["PENDING", "غير محدد", "N/A"]:
                buttons.append([
                    InlineKeyboardButton(text="💳 تحديد حساب استلام الليرة", callback_data=f"set_seller_info_{deal.id}")
                ])
            else:
                buttons.append([
                    InlineKeyboardButton(text="⏳ متابعة مرحلة الدفع", callback_data=f"man_aw_syp_{deal.id}")
                ])

    elif status == "AWAITING_SYP_PAYMENT":
        if is_buyer:
            buttons.append([
                InlineKeyboardButton(text="✅ تأكيد تحويل الليرة", callback_data=f"man_syp_{deal.id}")
            ])

    elif status == "SYP_PAYMENT_SENT":
        if is_seller:
            buttons.append([
                InlineKeyboardButton(text="✅ تأكيد استلام الليرة", callback_data=f"ask_conf_{deal.id}")
            ])

    elif status == "SYP_PAYMENT_CONFIRMED":
        if is_seller:
            buttons.append([
                InlineKeyboardButton(text="🛡️ طلب اعتماد الإدارة", callback_data=f"man_adm_appr_{deal.id}")
            ])

    elif status == "ADMIN_APPROVAL":
        if is_admin:
            buttons.append([
                InlineKeyboardButton(text="🚀 اعتماد وإطلاق الضمان", callback_data=f"ask_rel_{deal.id}"),
                InlineKeyboardButton(text="🔍 فحص تفصيلي للصفقة", callback_data=f"adm_inspect_deal_{deal.id}")
            ])

    if not buttons:
        return None
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def render_deal_card(
    deal: Deal,
    viewer_id: int,
    is_offer_owner: bool = False
) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
    """
    Renders pure markdown text and action keyboard for a single counterparty card.
    Adheres strictly to the 7-step layout, progress indicator, and safe escaping.
    """
    is_seller = (deal.seller_id == viewer_id)
    is_buyer = (deal.buyer_id == viewer_id)
    is_admin = (viewer_id in settings.ADMIN_IDS)

    if is_seller:
        viewer_role = "seller"
        role_label = "🔴 بائع USDT"
    elif is_buyer:
        viewer_role = "buyer"
        role_label = "🟢 مشتري USDT"
    elif is_admin:
        viewer_role = "admin"
        role_label = "🛡️ مشرف المنصة"
    else:
        viewer_role = "guest"
        role_label = "متابع"

    step_num, progress_bar, step_name = step_of(deal.status)
    is_turn, turn_desc = turn_of(deal.status, viewer_role, is_offer_owner=is_offer_owner)

    usdt_str = format_usdt(deal.amount_usdt)
    syp_str = format_syp(deal.amount_syp)

    # Effective rate
    if deal.amount_usdt and deal.amount_usdt > 0:
        rate = (deal.amount_syp / deal.amount_usdt).quantize(Decimal("1"))
        rate_str = format_syp(rate)
    else:
        rate_str = "غير محدد"

    lines = [
        f"🤝 **بطاقة صفقة الضمان #{deal.id}**",
        "━━━━━━━━━━━━━━━",
    ]

    if step_num > 0:
        lines.append(f"📍 **المرحلة:** {progress_bar} (الخطوة {step_num} من 7)")
        lines.append(f"📌 **المرحلة الحالية:** {step_name}")
    else:
        lines.append(f"📍 **الحالة النهائية:** {progress_bar} {step_name}")

    # Turn notification indicator
    if is_turn:
        lines.append(f"🎯 **دورك الآن:** {turn_desc}")
    else:
        lines.append(f"⏳ **الوضع:** {turn_desc}")

    # Expiry remaining time if active
    if deal.expires_at and deal.status in ("PENDING_OWNER_APPROVAL", "AWAITING_USDT_DEPOSIT", "CREATED"):
        remaining_str = humanize_time_remaining(deal.expires_at)
        lines.append(f"⏱️ **المهلة المتبقية:** {remaining_str}")

    lines.append("━━━━━━━━━━━━━━━")
    lines.append(f"👤 **دورك بالصفقة:** {role_label}")
    lines.append(f"💰 **الكمية:** `{usdt_str}` USDT")
    lines.append(f"💵 **الإجمالي:** `{syp_str}` SYP")
    lines.append(f"📊 **سعر الصرف:** `{rate_str}` SYP")

    # Specific phase context
    if deal.status in ("CREATED", "AWAITING_USDT_DEPOSIT") and is_seller:
        fee_str = format_usdt(deal.fee_usdt)
        total_dep = format_usdt(deal.amount_usdt + (deal.fee_usdt or Decimal("0")))
        lines.append("━━━━━━━━━━━━━━━")
        lines.append(f"💳 **المطلوب إيداعه:** `{total_dep}` USDT (تشمل عمولة `{fee_str}` USDT)")
        lines.append(f"📥 **محفظة الإيداع:** `{deal.deposit_address}`")

    elif deal.status in ("AWAITING_SYP_PAYMENT", "SYP_PAYMENT_SENT", "SYP_PAYMENT_CONFIRMED", "ADMIN_APPROVAL"):
        seller_method = "غير محدد"
        seller_info_str = "غير محدد"
        if isinstance(deal.seller_payment_info, dict):
            raw_method = deal.seller_payment_info.get("method", "غير محدد")
            icon = PAYMENT_METHOD_ICONS.get(raw_method, "💳")
            seller_method = f"{icon} {raw_method}"
            seller_info_str = deal.seller_payment_info.get("info", "غير محدد")
        elif deal.seller_payment_info:
            seller_info_str = str(deal.seller_payment_info)

        lines.append("━━━━━━━━━━━━━━━")
        lines.append(f"🏦 **وسيلة الدفع:** {seller_method}")
        lines.append(f"📋 **حساب استلام البائع:** `{escape_markdown(seller_info_str)}`")

    if deal.status in ("ADMIN_APPROVAL", "COMPLETED") and deal.buyer_payout_address:
        lines.append(f"📥 **محفظة استلام المشتري:** `{deal.buyer_payout_address}`")

    lines.append("━━━━━━━━━━━━━━━")

    # Final guidance message
    if deal.status == "COMPLETED":
        lines.append("🎉 **تم اكتمال الصفقة بنجاح وإطلاق الضمان لمستحقه.**")
    elif deal.status == "CANCELLED":
        lines.append("❌ **تم إلغاء هذه الصفقة وإغلاقها.**")
    elif deal.status == "EXPIRED":
        lines.append("⌛ **انتهت مهلة استكمال الصفقة وتم إلغاؤها تلقائياً.**")
    elif is_turn:
        lines.append("👇 **يرجى اتخاذ الإجراء المطلوب أدناه للمتابعة:**")
    else:
        lines.append("💡 سيتم تحديث هذه البطاقة تلقائياً فور استجابة الطرف الآخر.")

    card_text = "\n".join(lines)
    keyboard = get_deal_action_keyboard(deal, viewer_id, is_offer_owner=is_offer_owner)
    return card_text, keyboard


async def push_deal_card(
    deal_id: int,
    bot: Bot,
    notify_handoff: bool = True,
    specific_user_id: Optional[int] = None
) -> None:
    """
    Orchestrates in-place updating of the single Deal Card for both counterparties.
    Executes in a dedicated, isolated database session post-commit.
    - If turn handed off: deletes previous message and sends new one with sound notification.
    - Otherwise: smoothly edits existing card message in-place without clutter.
    - Captures and handles all Telegram exceptions cleanly.
    """
    try:
        async with AsyncSessionLocal() as session:
            res = await session.execute(
                select(Deal, Offer)
                .outerjoin(Offer, Deal.offer_id == Offer.id)
                .where(Deal.id == deal_id)
            )
            row = res.first()
            if not row:
                logger.warning(f"[DealCard] Deal #{deal_id} not found during push_deal_card.")
                return

            deal, offer = row
            maker_id = offer.user_id if offer else None

            parties = []
            if deal.buyer_id and (specific_user_id is None or specific_user_id == deal.buyer_id):
                parties.append(("buyer", deal.buyer_id, deal.buyer_card_msg_id))
            if deal.seller_id and (specific_user_id is None or specific_user_id == deal.seller_id):
                parties.append(("seller", deal.seller_id, deal.seller_card_msg_id))

            for role, user_id, current_msg_id in parties:
                is_offer_owner = (maker_id is not None and user_id == maker_id)
                card_text, kb = render_deal_card(deal, user_id, is_offer_owner=is_offer_owner)
                is_my_turn, _ = turn_of(deal.status, role, is_offer_owner=is_offer_owner)

                new_msg_id = None
                # Check if we should notify with a new message (turn handoff)
                should_alert = notify_handoff and is_my_turn and current_msg_id is not None

                if should_alert:
                    # Attempt to delete old message to keep chat spotless
                    try:
                        await bot.delete_message(chat_id=user_id, message_id=current_msg_id)
                    except Exception:
                        pass  # Deletion may fail if message is too old or user deleted it
                    
                    try:
                        sent_msg = await bot.send_message(
                            chat_id=user_id,
                            text=card_text,
                            reply_markup=kb,
                            parse_mode="Markdown"
                        )
                        new_msg_id = sent_msg.message_id
                    except (TelegramForbiddenError, TelegramBadRequest) as e:
                        logger.warning(f"[DealCard] Failed to alert {role} ({user_id}) for deal #{deal_id}: {e}")
                elif current_msg_id is not None:
                    try:
                        await bot.edit_message_text(
                            text=card_text,
                            chat_id=user_id,
                            message_id=current_msg_id,
                            reply_markup=kb,
                            parse_mode="Markdown"
                        )
                    except TelegramBadRequest as e:
                        err_desc = str(e).lower()
                        if "message is not modified" in err_desc:
                            pass
                        elif "message to edit not found" in err_desc or "cant be edited" in err_desc:
                            # Send replacement message
                            try:
                                sent_msg = await bot.send_message(
                                    chat_id=user_id,
                                    text=card_text,
                                    reply_markup=kb,
                                    parse_mode="Markdown"
                                )
                                new_msg_id = sent_msg.message_id
                            except Exception as send_err:
                                logger.warning(f"[DealCard] Fallback send failed for {role} ({user_id}): {send_err}")
                        else:
                            logger.warning(f"[DealCard] edit_message_text error for {role} ({user_id}): {e}")
                    except TelegramForbiddenError as e:
                        logger.warning(f"[DealCard] Bot blocked by {role} ({user_id}): {e}")
                else:
                    # Initial card dispatch
                    try:
                        sent_msg = await bot.send_message(
                            chat_id=user_id,
                            text=card_text,
                            reply_markup=kb,
                            parse_mode="Markdown"
                        )
                        new_msg_id = sent_msg.message_id
                    except (TelegramForbiddenError, TelegramBadRequest) as e:
                        logger.warning(f"[DealCard] Initial card send failed for {role} ({user_id}): {e}")

                # Update recorded message ID if changed
                if new_msg_id and new_msg_id != current_msg_id:
                    col = Deal.buyer_card_msg_id if role == "buyer" else Deal.seller_card_msg_id
                    await session.execute(
                        update(Deal).where(Deal.id == deal_id).values({col: new_msg_id})
                    )
                    await session.commit()

    except Exception as outer_err:
        logger.error(f"[DealCard] Unexpected exception in push_deal_card for deal #{deal_id}: {outer_err}", exc_info=True)
