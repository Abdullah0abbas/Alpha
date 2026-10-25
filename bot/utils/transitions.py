"""
Pure state transition validators for Alpha Bot.
Contains zero database or Telegram dependencies to enable fast, deterministic unit testing.
"""
from datetime import datetime, timezone
from typing import Optional, Tuple


def can_expire_deal(
    current_status: str,
    expires_at: Optional[datetime],
    now: Optional[datetime] = None
) -> bool:
    """
    Checks if a deal is in an expirable status and its timeout has elapsed.
    Valid expirable statuses: PENDING_OWNER_APPROVAL, AWAITING_USDT_DEPOSIT, CREATED.
    """
    if current_status not in ("PENDING_OWNER_APPROVAL", "AWAITING_USDT_DEPOSIT", "CREATED"):
        return False
    if expires_at is None:
        return False
    current_time = now or datetime.now(timezone.utc)
    return expires_at <= current_time


def can_enter_grace_period(
    current_status: str,
    expires_at: Optional[datetime],
    now: Optional[datetime] = None
) -> bool:
    """
    Checks if an offer can transition from 'active' to 'expired_grace'.
    Offers in 'reserved', 'negotiating', 'cancelled', 'expired', 'completed' are strictly disallowed.
    """
    if current_status != "active":
        return False
    if expires_at is None:
        return False
    current_time = now or datetime.now(timezone.utc)
    return expires_at <= current_time


def can_permanently_expire_offer(
    current_status: str,
    expires_at: Optional[datetime],
    now: Optional[datetime] = None
) -> bool:
    """
    Checks if an offer in 'expired_grace' has exhausted its 30-min grace period and must be archived as 'expired'.
    """
    if current_status != "expired_grace":
        return False
    if expires_at is None:
        return False
    current_time = now or datetime.now(timezone.utc)
    return expires_at <= current_time


def can_expire_negotiation(
    current_status: str,
    expires_at: Optional[datetime],
    now: Optional[datetime] = None
) -> bool:
    """
    Checks if a negotiation in PENDING_MAKER or PENDING_TAKER has elapsed.
    """
    if current_status not in ("PENDING_MAKER", "PENDING_TAKER"):
        return False
    if expires_at is None:
        return False
    current_time = now or datetime.now(timezone.utc)
    return expires_at <= current_time


def can_extend_offer(current_status: str) -> Tuple[bool, str]:
    """
    Validates if an offer can be extended.
    Allowed only when status is 'active' or 'expired_grace'.
    Returns (can_extend, reason_ar).
    """
    if current_status in ("active", "expired_grace"):
        return True, ""
    if current_status == "reserved":
        return False, "هذا العرض مرتبط بصفقة قيد التنفيذ حالياً."
    if current_status == "negotiating":
        return False, "هذا العرض مرتبط بجلسة تفاوض نشطة حالياً."
    if current_status in ("cancelled", "expired", "completed"):
        return False, "عذراً، هذا العرض منتهي أو تم حذفه مسبقاً ولا يمكن تمديده."
    return False, "عذراً، حالة العرض الحالية لا تسمح بتمديده."


def can_delete_offer(current_status: str) -> Tuple[bool, str]:
    """
    Validates if an offer can be cancelled/deleted by maker/admin.
    Allowed when status is 'active' or 'expired_grace'.
    Returns (can_delete, reason_ar).
    """
    if current_status in ("active", "expired_grace"):
        return True, ""
    if current_status == "reserved":
        return False, "لا يمكن إلغاء العرض لوجود صفقة جارية عليه حالياً."
    if current_status == "negotiating":
        return False, "لا يمكن إلغاء العرض لوجود جلسة تفاوض جارية عليه حالياً."
    if current_status in ("cancelled", "expired", "completed"):
        return False, "تم إنهاء أو حذف هذا العرض مسبقاً."
    return False, "عذراً، حالة العرض الحالية لا تسمح بإلغائه."


def can_maker_reject_negotiation(current_status: str) -> bool:
    """
    Maker can only reject when negotiation is in PENDING_MAKER.
    """
    return current_status == "PENDING_MAKER"


def can_taker_reject_negotiation(current_status: str) -> bool:
    """
    Taker can only reject counter when negotiation is in PENDING_TAKER.
    """
    return current_status == "PENDING_TAKER"


def can_maker_counter_negotiation(
    current_status: str,
    expires_at: Optional[datetime],
    now: Optional[datetime] = None
) -> bool:
    """
    Maker can submit counter only if status is PENDING_MAKER and not expired.
    """
    if current_status != "PENDING_MAKER":
        return False
    if expires_at is not None:
        current_time = now or datetime.now(timezone.utc)
        if expires_at <= current_time:
            return False
    return True


def resolve_deal_parties(
    maker_id: Optional[int],
    seller_id: int,
    buyer_id: int
) -> Tuple[Optional[int], Optional[int]]:
    """
    Resolves (maker_id, taker_id) from deal counterparties and offer creator user_id.
    - If maker_id is the seller, taker_id is the buyer.
    - Otherwise (maker is buyer), taker_id is the seller.
    - If maker_id is None (e.g. associated offer not found), returns (None, None).
    """
    if maker_id is None:
        return None, None
    taker_id = buyer_id if maker_id == seller_id else seller_id
    return maker_id, taker_id

