from typing import Tuple, Optional, Union, List, Set, Dict
import logging
import time
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, or_, func

from db.models import Deal
from config.settings import settings

logger = logging.getLogger("deal_guard")

MAX_ACTIVE_DEALS = 5
TERMINAL_DEAL_STATUSES = ("COMPLETED", "CANCELLED", "EXPIRED", "DISPUTED")

# Cooldown map: (owner_id, offer_id) -> monotonic timestamp of last notification
_OWNER_MAX_DEALS_ALERT_COOLDOWN_SEC = 300.0
_owner_max_deals_last_alert: Dict[Tuple[int, int], float] = {}


async def count_user_active_deals(db_session: AsyncSession, user_id: int) -> int:
    """Returns the count of currently active (incomplete) deals where user_id is seller or buyer."""
    res = await db_session.execute(
        select(func.count(Deal.id)).where(
            or_(Deal.seller_id == user_id, Deal.buyer_id == user_id),
            Deal.status.not_in(TERMINAL_DEAL_STATUSES),
        )
    )
    return int(res.scalar() or 0)


def build_max_active_deals_kb() -> InlineKeyboardMarkup:
    """Returns an inline keyboard leading directly to the user's active deals list."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💼 صفقاتي", callback_data="main_my_deals")],
            [InlineKeyboardButton(text="🏠 الرئيسية", callback_data="menu_back_main")],
        ]
    )


def build_max_active_deals_notice_text(for_offer_id: Optional[int] = None) -> str:
    """Builds the Arabic notification message when a user has reached 5 incomplete deals."""
    if for_offer_id is not None:
        return (
            f"⚠️ **إشعار بخصوص عرضك رقم #{for_offer_id}**\n\n"
            f"حاول أحد المتداولين التقديم على عرضك، ولكن لديك **{MAX_ACTIVE_DEALS} صفقات غير مكتملة** حالياً.\n"
            "يرجى متابعتهم (إلغاء أو استكمال) لتتمكن من استقبال وإجراء معاملات جديدة."
        )
    return (
        f"⚠️ **لديك {MAX_ACTIVE_DEALS} صفقات غير مكتملة حالياً**\n\n"
        "يرجى متابعتهم (إلغاء أو استكمال) لتتمكن من إجراء معاملات جديدة."
    )


async def notify_offer_owner_max_deals(bot: Optional[Bot], owner_id: int, offer_id: int) -> None:
    """
    Sends a rate-limited notification to an offer owner when a customer attempts to interact
    with their offer while the owner already has 5 incomplete deals.
    """
    if not bot or not owner_id:
        return
    now_mono = time.monotonic()
    key = (owner_id, offer_id)
    last_ts = _owner_max_deals_last_alert.get(key)
    if last_ts is not None and (now_mono - last_ts) < _OWNER_MAX_DEALS_ALERT_COOLDOWN_SEC:
        return
    _owner_max_deals_last_alert[key] = now_mono
    try:
        await bot.send_message(
            chat_id=owner_id,
            text=build_max_active_deals_notice_text(for_offer_id=offer_id),
            reply_markup=build_max_active_deals_kb(),
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.warning(f"Failed to send 5-active-deals alert to offer owner {owner_id} for offer #{offer_id}: {e}")

STATUS_ARABIC = {
    "PENDING_OWNER_APPROVAL": "بانتظار موافقة الطرف الآخر",
    "CREATED": "قيد الإنشاء",
    "AWAITING_USDT_DEPOSIT": "بانتظار إيداع الضمان",
    "USDT_DEPOSITED": "الضمان محجوز",
    "AWAITING_SYP_PAYMENT": "بانتظار دفع الليرة",
    "SYP_PAYMENT_SENT": "تم تحويل الليرة — بانتظار التأكيد",
    "SYP_PAYMENT_CONFIRMED": "تم تأكيد الاستلام — بانتظار الاعتماد",
    "ADMIN_APPROVAL": "قيد التدقيق والاعتماد الإداري",
    "RELEASING": "قيد إطلاق الضمان",
    "COMPLETED": "مكتملة",
    "CANCELLED": "ملغاة",
    "DISPUTED": "قيد النزاع",
    "EXPIRED": "منتهية الصلاحية",
}

async def acquire_deal_lock(
    deal_id: int,
    user_id: int,
    expected_statuses: Union[str, List[str], Set[str]],
    allowed_roles: Union[str, List[str]],
    db_session: AsyncSession,
) -> Tuple[bool, Optional[Deal], str, str]:
    """
    Acquires a row-level lock on Deal with FOR UPDATE, validates authorization and status.

    Returns:
        (is_valid, deal, error_code, error_message_ar)
    
    Error codes:
        - "NOT_FOUND": Deal does not exist.
        - "UNAUTHORIZED": User does not match allowed roles.
        - "ALREADY_PROCESSED": State already advanced beyond expected statuses (idempotency).
        - "INVALID_STATE": Deal is in an unexpected/cancelled/expired state.
    """
    if isinstance(expected_statuses, str):
        expected_set = {expected_statuses}
    else:
        expected_set = set(expected_statuses)

    if isinstance(allowed_roles, str):
        allowed_roles_list = [allowed_roles]
    else:
        allowed_roles_list = list(allowed_roles)

    result = await db_session.execute(
        select(Deal).where(Deal.id == deal_id).with_for_update()
    )
    deal = result.scalar_one_or_none()

    if not deal:
        return False, None, "NOT_FOUND", "عذراً، لم يتم العثور على الصفقة المطلوبة."

    # Authorization Check
    is_admin = user_id in settings.ADMIN_IDS
    is_seller = (user_id == deal.seller_id)
    is_buyer = (user_id == deal.buyer_id)

    role_authorized = False
    for role in allowed_roles_list:
        if role == "seller" and is_seller:
            role_authorized = True
            break
        elif role == "buyer" and is_buyer:
            role_authorized = True
            break
        elif role == "admin" and is_admin:
            role_authorized = True
            break
        elif role in ("any_party", "participant") and (is_seller or is_buyer or is_admin):
            role_authorized = True
            break

    if not role_authorized:
        return False, deal, "UNAUTHORIZED", "غير مصرح: هذا الإجراء ليس من صلاحيتك بهذه الصفقة."

    # Status Validation
    if deal.status in expected_set:
        return True, deal, "", ""

    # Status Sequence Map for Idempotency Detection
    # If the deal has already progressed forward from the expected state
    STATUS_ORDER = {
        "PENDING_OWNER_APPROVAL": 1,
        "CREATED": 2,
        "AWAITING_USDT_DEPOSIT": 3,
        "USDT_DEPOSITED": 4,
        "AWAITING_SYP_PAYMENT": 5,
        "SYP_PAYMENT_SENT": 6,
        "SYP_PAYMENT_CONFIRMED": 7,
        "ADMIN_APPROVAL": 8,
        "RELEASING": 9,
        "COMPLETED": 10,
        "EXPIRED": 97,
        "DISPUTED": 98,
        "CANCELLED": 99,
    }

    current_order = STATUS_ORDER.get(deal.status, 0)
    max_expected_order = max((STATUS_ORDER.get(s, 0) for s in expected_set), default=0)

    if deal.status in ("CANCELLED", "DISPUTED", "EXPIRED"):
        status_name = STATUS_ARABIC.get(deal.status, deal.status)
        return False, deal, "INVALID_STATE", f"⚠️ لا يمكن المتابعة: الصفقة {status_name} حالياً."

    if current_order > max_expected_order and current_order < 90:
        return False, deal, "ALREADY_PROCESSED", "ℹ️ تم تنفيذ هذا الإجراء مسبقاً، والصفقة في مرحلة متقدمة."

    status_name = STATUS_ARABIC.get(deal.status, deal.status)
    return False, deal, "INVALID_STATE", f"⚠️ حالة الصفقة الحالية ({status_name}) لا تسمح بتنفيذ هذا الإجراء."
