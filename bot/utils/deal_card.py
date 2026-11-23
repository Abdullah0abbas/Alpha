"""
Unified Deal Card module for Alpha Bot.
Provides pure rendering, step & turn tracking, keyboard generation, and
robust Telegram in-place update orchestration (push_deal_card).
"""
import logging
from decimal import Decimal
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
        return 1, "[ 🟩 ⬜ ⬜ ⬜ ⬜ ⬜ ⬜ ]", "موافقة صانع العرض"
    elif status in ("CREATED", "AWAITING_USDT_DEPOSIT"):
        return 2, "[ 🟩 🟩 ⬜ ⬜ ⬜ ⬜ ⬜ ]", "إيداع رصيد الضمان"
    elif status == "USDT_DEPOSITED":
        return 3, "[ 🟩 🟩 🟩 ⬜ ⬜ ⬜ ⬜ ]", "حساب الاستلام"
    elif status == "AWAITING_SYP_PAYMENT":
        return 4, "[ 🟩 🟩 🟩 🟩 ⬜ ⬜ ⬜ ]", "تحويل المبلغ بالليرة السورية"
    elif status == "SYP_PAYMENT_SENT":
        return 5, "[ 🟩 🟩 🟩 🟩 🟩 ⬜ ⬜ ]", "تأكيد الاستلام بالليرة السورية"
    elif status in ("SYP_PAYMENT_CONFIRMED", "ADMIN_APPROVAL"):
        return 6, "[ 🟩 🟩 🟩 🟩 🟩 🟩 ⬜ ]", "تدقيق واعتماد الإدارة"
    elif status == "RELEASING":
        return 6, "[ 🟩 🟩 🟩 🟩 🟩 🟩 ⏳ ]", "جاري تحرير الرصيد على الشبكة"
    elif status == "COMPLETED":
        return 7, "[ 🟩 🟩 🟩 🟩 🟩 🟩 🟩 ]", "تحرير الرصيد"
    elif status == "CANCELLED":
        return 0, "[ ❌ ❌ ❌ ❌ ❌ ❌ ❌ ]", "ملغاة"
    elif status == "EXPIRED":
        return 0, "[ ⌛ ⌛ ⌛ ⌛ ⌛ ⌛ ⌛ ]", "منتهية"
    elif status == "DISPUTED":
        return 0, "[ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ⚠️ ]", "مغلقة — متابعة يدوية عبر الدعم الفني"
    return 0, "[ ⬜ ⬜ ⬜ ⬜ ⬜ ⬜ ⬜ ]", status


def turn_of(status: str, viewer_role: str, is_offer_owner: bool = False) -> Tuple[bool, str]:
    """
    Calculates if it is currently viewer's turn, along with an Arabic status description.
    viewer_role: 'seller' | 'buyer' | 'admin'
    """
    if status == "PENDING_OWNER_APPROVAL":
        if is_offer_owner:
            return True, "الموافقة على طلب الصفقة"
        return False, "بانتظار موافقة صانع العرض"

    if status in ("CREATED", "AWAITING_USDT_DEPOSIT"):
        if viewer_role == "seller":
            return True, "تحويل رصيد الضمان إلى محفظة الإيداع"
        return False, "بانتظار إيداع رصيد الضمان من البائع"

    if status == "USDT_DEPOSITED":
        if viewer_role == "seller":
            return True, "تأكيد حساب الاستلام بالليرة السورية"
        return False, "بانتظار تأكيد حساب الاستلام من البائع"

    if status == "AWAITING_SYP_PAYMENT":
        if viewer_role == "buyer":
            return True, "تحويل المبلغ وإرفاق إشعار الدفع"
        return False, "بانتظار تحويل المبلغ بالليرة السورية"

    if status == "SYP_PAYMENT_SENT":
        if viewer_role == "seller":
            return True, "التحقق وتأكيد استلام المبلغ بالليرة السورية"
        return False, "بانتظار تأكيد البائع لاستلام الدفعة"

    if status in ("SYP_PAYMENT_CONFIRMED", "ADMIN_APPROVAL"):
        if viewer_role == "admin":
            return True, "اعتماد وتحرير الرصيد للمشتري"
        return False, "بانتظار اعتماد الإدارة وتحرير الرصيد"

    if status == "RELEASING":
        return False, "جاري تحرير الرصيد على الشبكة ⏳"

    if status == "COMPLETED":
        return False, "اكتملت الصفقة وتحرر الرصيد بنجاح"

    if status == "CANCELLED":
        return False, "تم إلغاء الصفقة"

    if status == "EXPIRED":
        return False, "انتهت مهلة الصفقة"

    if status == "DISPUTED":
        return False, "أُغلقت الصفقة آلياً — قيد المتابعة اليدوية مع الدعم الفني"

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
            buttons.append([
                InlineKeyboardButton(text="❌ إلغاء الطلب", callback_data=f"ask_cancel_{deal.id}")
            ])

    elif status == "CREATED":
        if is_seller and not is_offer_owner:
            buttons.append([
                InlineKeyboardButton(text="🔐 إيداع الضمان", callback_data=f"man_aw_dep_{deal.id}")
            ])
            buttons.append([
                InlineKeyboardButton(text="❌ إلغاء", callback_data=f"ask_cancel_{deal.id}")
            ])
        elif is_seller and is_offer_owner:
            buttons.append([
                InlineKeyboardButton(text="🔐 إيداع الضمان", callback_data=f"man_aw_dep_{deal.id}")
            ])
        elif is_buyer:
            buttons.append([
                InlineKeyboardButton(text="❌ إلغاء", callback_data=f"ask_cancel_{deal.id}")
            ])

    elif status == "AWAITING_USDT_DEPOSIT":
        if is_seller and not is_offer_owner:
            buttons.append([
                InlineKeyboardButton(text="✅ تم الإيداع", callback_data=f"man_dep_{deal.id}")
            ])
            buttons.append([
                InlineKeyboardButton(text="❌ إلغاء", callback_data=f"ask_cancel_{deal.id}")
            ])
        elif is_seller and is_offer_owner:
            buttons.append([
                InlineKeyboardButton(text="✅ تم الإيداع", callback_data=f"man_dep_{deal.id}")
            ])
        elif is_buyer:
            buttons.append([
                InlineKeyboardButton(text="❌ إلغاء", callback_data=f"ask_cancel_{deal.id}")
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
                    InlineKeyboardButton(text="💳 تحديد حساب الاستلام", callback_data=f"set_seller_info_{deal.id}")
                ])
            else:
                buttons.append([
                    InlineKeyboardButton(text="▶️ متابعة الدفع", callback_data=f"man_aw_syp_{deal.id}")
                ])

    elif status == "AWAITING_SYP_PAYMENT":
        if is_buyer:
            buttons.append([
                InlineKeyboardButton(text="📸 إرفاق إشعار الدفع", callback_data=f"man_syp_{deal.id}")
            ])

    elif status == "SYP_PAYMENT_SENT":
        if is_seller:
            buttons.append([
                InlineKeyboardButton(text="✅ تأكيد الاستلام", callback_data=f"ask_conf_{deal.id}"),
                InlineKeyboardButton(text="⚠️ لم أستلم", callback_data=f"dispute_deal_{deal.id}")
            ])
        elif is_buyer:
            buyer_addr = (getattr(deal, "buyer_payout_address", None) or "").strip()
            if not buyer_addr or buyer_addr in ("PENDING", "غير محدد", "N/A"):
                buttons.append([
                    InlineKeyboardButton(text="💵 إدخال عنوان USDT", callback_data=f"set_buyer_payout_{deal.id}")
                ])

    elif status == "SYP_PAYMENT_CONFIRMED":
        if is_seller:
            buttons.append([
                InlineKeyboardButton(text="🛡️ طلب اعتماد الإدارة", callback_data=f"man_adm_appr_{deal.id}")
            ])

    elif status == "ADMIN_APPROVAL":
        if is_admin:
            buttons.append([
                InlineKeyboardButton(text="☑️ اعتماد وتحرير الرصيد", callback_data=f"ask_rel_{deal.id}"),
                InlineKeyboardButton(text="🔍 تفاصيل الصفقة", callback_data=f"adm_inspect_deal_{deal.id}")
            ])

    elif status == "DISPUTED":
        support_handle = (settings.SUPPORT_USERNAME or "AlphaSupport").lstrip('@')
        buttons.append([
            InlineKeyboardButton(text="👨‍💻 الدعم الفني", url=f"https://t.me/{support_handle}")
        ])
        if is_admin:
            buttons.append([
                InlineKeyboardButton(text="🔍 تفاصيل الصفقة", callback_data=f"adm_inspect_deal_{deal.id}")
            ])

    elif status == "COMPLETED":
        payout_tx = (getattr(deal, "payout_tx_hash", None) or "").strip()
        if payout_tx.startswith("0x") and len(payout_tx) == 66:
            buttons.append([
                InlineKeyboardButton(text="🔗 عرض المعاملة على BscScan", url=f"https://bscscan.com/tx/{payout_tx}")
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
        role_label = "🔴 بائع"
    elif is_buyer:
        viewer_role = "buyer"
        role_label = "🟢 مشتري"
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

    if deal.amount_usdt and deal.amount_usdt > 0:
        rate = deal.amount_syp / deal.amount_usdt
        rate_str = format_syp(rate)
    else:
        rate_str = "غير محدد"

    lines = [
        f"🧾 **بطاقة الصفقة #{deal.id}**",
    ]
    if getattr(deal, "offer_id", None):
        lines.append(f"📦 **رقم العرض:** #{deal.offer_id}")

    if step_num > 0:
        lines.append(f"📍 **المرحلة:** {progress_bar} (الخطوة {step_num} من 7)")
        lines.append(f"📌 **الخطوة الحالية:** {step_name}")
    else:
        lines.append(f"📍 **الحالة:** {progress_bar} {step_name}")

    if is_turn:
        lines.append(f"🎯 **دورك الآن:** {turn_desc}")
    else:
        lines.append(f"⏳ **الوضع الحالي:** {turn_desc}")

    if deal.expires_at and deal.status in (
        "PENDING_OWNER_APPROVAL",
        "AWAITING_USDT_DEPOSIT",
        "CREATED",
        "USDT_DEPOSITED",
        "AWAITING_SYP_PAYMENT",
        "SYP_PAYMENT_SENT",
    ):
        remaining_str = humanize_time_remaining(deal.expires_at)
        lines.append(f"⏳ **المهلة المتبقية:** {remaining_str}")

    lines.append("〰️〰️〰️〰️〰️〰️〰️")
    lines.append(f"👤 **دورك:** {role_label}")
    lines.append(f"💰 **الكمية:** `{usdt_str} USDT`")
    lines.append(f"💵 **السعر:** `{rate_str}` ل.س")
    lines.append(f"💴 **المبلغ المطلوب:** `{syp_str}` ل.س")

    seller_fee = getattr(deal, "seller_fee_usdt", None)
    buyer_fee = getattr(deal, "buyer_fee_usdt", None)
    if seller_fee is None or buyer_fee is None:
        from bot.utils.fees import calculate_deal_fee
        fee_info = calculate_deal_fee(deal.amount_usdt)
        seller_fee = fee_info.seller_fee
        buyer_fee = fee_info.buyer_fee

    if is_buyer:
        net_buyer_str = format_usdt(deal.amount_usdt - buyer_fee)
        if buyer_fee > Decimal("0"):
            lines.append(f"📥 **صافي الاستلام:** `{net_buyer_str} USDT` (العمولة: `{format_usdt(buyer_fee)} USDT`)")
        else:
            lines.append(f"📥 **صافي الاستلام:** `{net_buyer_str} USDT` (العمولة: مجانية)")

    if deal.status in ("CREATED", "AWAITING_USDT_DEPOSIT") and is_seller:
        total_dep = deal.amount_usdt + seller_fee
        total_dep_str = format_usdt(total_dep)
        lines.append("〰️〰️〰️〰️〰️〰️〰️")
        if seller_fee > Decimal("0"):
            lines.append(f"🔐 **المطلوب إيداعه:** `{total_dep_str} USDT` (العمولة: `{format_usdt(seller_fee)} USDT`)")
        else:
            lines.append(f"🔐 **المطلوب إيداعه:** `{total_dep_str} USDT` (العمولة: مجانية)")
        lines.append("🌐 **الشبكة:** `BEP-20`")
        if deal.status == "AWAITING_USDT_DEPOSIT":
            lines.append(f"📥 **عنوان الإيداع:**\n`{escape_markdown(deal.deposit_address)}`")
        else:
            lines.append("🔒 **عنوان الإيداع:** سيظهر فور الضغط على زر «🔐 إيداع الضمان» وبدء مهلة الإيداع (15 دقيقة).")

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

        lines.append("〰️〰️〰️〰️〰️〰️〰️")
        if deal.status == "AWAITING_SYP_PAYMENT" and is_buyer:
            lines.append(f"✅ **تم التحقق من دفع `{usdt_str} USDT`**")
        lines.append(f"💳 **طريقة الدفع:** {seller_method}")
        lines.append(f"👤 **معلومات حساب البائع:**\n`{escape_markdown(seller_info_str)}`")
        if deal.status == "AWAITING_SYP_PAYMENT" and is_buyer:
            lines.append("⚠️ التزم بتحويل المبلغ المحدد بدقة ثم أرفق لقطة شاشة تثبت التحويل.")

    if deal.status in ("ADMIN_APPROVAL", "COMPLETED") and deal.buyer_payout_address:
        lines.append("〰️〰️〰️〰️〰️〰️〰️")
        lines.append(f"💵 **عنوان USDT (BEP-20):**\n`{escape_markdown(deal.buyer_payout_address)}`")
        payout_tx = (getattr(deal, "payout_tx_hash", None) or "").strip()
        if payout_tx:
            lines.append(f"🔗 **رمز معاملة الإطلاق (TxHash):**\n`{escape_markdown(payout_tx)}`")

    lines.append("")

    if deal.status == "COMPLETED":
        lines.append("☑️ **اكتملت الصفقة بنجاح وتم تحرير رصيد الـ USDT.**")
    elif deal.status == "CANCELLED":
        lines.append("❌ **تم إلغاء الصفقة.**")
    elif deal.status == "EXPIRED":
        lines.append("⌛ **انتهت مهلة الصفقة وتم إلغاؤها تلقائياً.**")
    elif deal.status == "DISPUTED":
        lines.append("⚠️ **أُغلقت الصفقة آلياً وتم تحويل الملف للمتابعة اليدوية عبر الدعم الفني.**")

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
                if role == "seller" and deal.status == "SYP_PAYMENT_SENT":
                    # The seller receives a single dedicated photo message with payment proof and confirmation buttons.
                    # Skip sending or editing text deal card to avoid duplicate messages.
                    continue
                if deal.status == "DISPUTED" and current_msg_id is None:
                    # Seller's photo caption (or direct notice) was already updated with the closure notice.
                    continue

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
                        elif "message to edit not found" in err_desc or "cant be edited" in err_desc or "there is no text in the message to edit" in err_desc:
                            # Send replacement message (preserve existing message if it is a photo/media message)
                            try:
                                if "there is no text in the message to edit" not in err_desc:
                                    try:
                                        await bot.delete_message(chat_id=user_id, message_id=current_msg_id)
                                    except Exception:
                                        pass
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
        try:
            from bot.middlewares.error_alert import report_handled_exception
            await report_handled_exception(
                bot,
                outer_err,
                context_label=f"تحديث بطاقة الصفقة push_deal_card (صفقة #{deal_id})",
                user_id=specific_user_id,
            )
        except Exception:
            pass
