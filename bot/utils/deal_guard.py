from typing import Tuple, Optional, Union, List, Set
import logging
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from db.models import Deal
from config.settings import settings

logger = logging.getLogger("deal_guard")

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
