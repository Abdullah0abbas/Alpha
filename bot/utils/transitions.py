"""
Pure state transition validators for Alpha Bot.
Contains zero database or Telegram dependencies to enable fast, deterministic unit testing.
"""
from datetime import datetime, timezone
from typing import Optional, Tuple

SELLER_INFO_ALLOWED_STATUSES = ("USDT_DEPOSITED",)


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


def can_expire_active_offer(
    current_status: str,
    expires_at: Optional[datetime],
    now: Optional[datetime] = None
) -> bool:
    """
    Checks if an active offer's duration has elapsed and should be immediately deleted ('deleted').
    Offers in 'reserved', 'negotiating', 'cancelled', 'expired', 'completed', 'deleted' are disallowed.
    """
    if current_status != "active":
        return False
    if expires_at is None:
        return False
    current_time = now or datetime.now(timezone.utc)
    return expires_at <= current_time


def can_remind_usdt_deposited(
    current_status: str,
    expires_at: Optional[datetime],
    now: Optional[datetime] = None
) -> bool:
    """
    Checks if a deal in USDT_DEPOSITED has reached its 30-min reminder timestamp
    without closing the deal (escrow funds remain safely held).
    """
    if current_status != "USDT_DEPOSITED":
        return False
    if expires_at is None:
        return False
    current_time = now or datetime.now(timezone.utc)
    return expires_at <= current_time


def can_remind_syp_payment(
    current_status: str,
    expires_at: Optional[datetime],
    now: Optional[datetime] = None
) -> bool:
    """
    Checks if a deal in AWAITING_SYP_PAYMENT has reached its 30-min reminder timestamp
    without closing the deal (escrow funds remain safely held).
    """
    if current_status != "AWAITING_SYP_PAYMENT":
        return False
    if expires_at is None:
        return False
    current_time = now or datetime.now(timezone.utc)
    return expires_at <= current_time


def can_remind_syp_confirmation(
    current_status: str,
    expires_at: Optional[datetime],
    now: Optional[datetime] = None
) -> bool:
    """
    Checks if a deal in SYP_PAYMENT_SENT has reached its 30-min seller confirmation
    reminder timestamp without closing the deal (escrow funds remain safely held).
    """
    if current_status != "SYP_PAYMENT_SENT":
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
    Allowed only when status is 'active'.
    Returns (can_extend, reason_ar).
    """
    if current_status == "active":
        return True, ""
    if current_status == "reserved":
        return False, "هذا العرض مرتبط بصفقة قيد التنفيذ حالياً."
    if current_status == "negotiating":
        return False, "هذا العرض مرتبط بجلسة تفاوض نشطة حالياً."
    if current_status in ("cancelled", "expired", "completed", "deleted", "expired_grace"):
        return False, "عذراً، هذا العرض منتهي أو تم حذفه مسبقاً ولا يمكن تمديده."
    return False, "عذراً، حالة العرض الحالية لا تسمح بتمديده."


def can_delete_offer(current_status: str) -> Tuple[bool, str]:
    """
    Validates if an offer can be cancelled/deleted by maker/admin.
    Allowed only when status is 'active'.
    Returns (can_delete, reason_ar).
    """
    if current_status == "active":
        return True, ""
    if current_status == "reserved":
        return False, "لا يمكن إلغاء العرض لوجود صفقة جارية عليه حالياً."
    if current_status == "negotiating":
        return False, "لا يمكن إلغاء العرض لوجود جلسة تفاوض جارية عليه حالياً."
    if current_status in ("cancelled", "expired", "completed", "deleted", "expired_grace"):
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


def can_cancel_deal(
    deal_status: str,
    user_id: int,
    seller_id: int,
    buyer_id: int,
    offer_owner_id: int,
    admin_ids: list[int] | set[int]
) -> Tuple[bool, str]:
    """Pure validator for deal cancellation authorization and status."""
    is_admin = user_id in admin_ids
    if user_id not in (seller_id, buyer_id) and not is_admin:
        return False, "غير مصرح: هذه ليست صفقتك."

    if not is_admin and user_id == offer_owner_id:
        if deal_status in ("CREATED", "AWAITING_USDT_DEPOSIT"):
            return False, "لا يمكنك إلغاء الصفقة بعد موافقتك عليها. إن لم يتم الإيداع خلال المهلة تنتهي الصفقة تلقائياً."

    if deal_status not in ("PENDING_OWNER_APPROVAL", "CREATED", "AWAITING_USDT_DEPOSIT"):
        return False, "لا يمكن إلغاء الصفقة في هذه المرحلة دون مراجعة الإدارة."

    return True, ""


def can_seller_confirm_syp(deal_status: str, user_id: int, seller_id: int) -> Tuple[bool, str]:
    """Pure validator for seller SYP payment confirmation."""
    if user_id != seller_id:
        return False, "غير مصرح: هذا الإجراء مخصص لبائع الصفقة فقط."
    if deal_status != "SYP_PAYMENT_SENT":
        return False, "لا يمكن تنفيذ هذا الإجراء في هذه المرحلة من الصفقة."
    return True, ""


def can_admin_release_escrow(deal_status: str, user_id: int, admin_ids: list[int] | set[int]) -> Tuple[bool, str]:
    """Pure validator for admin escrow release."""
    if user_id not in admin_ids:
        return False, "غير مصرح: هذه الصلاحية للإدارة فقط."
    if deal_status != "ADMIN_APPROVAL":
        return False, "لا يمكن إطلاق هذه الصفقة — لم تصل بعد لمرحلة الاعتماد الإداري."
    return True, ""


def get_next_deal_status_after_syp_confirmed(current_status: str) -> Optional[str]:
    """Returns next state for auto-dispatching to admin approval upon seller confirmation."""
    if current_status in ("SYP_PAYMENT_SENT", "SYP_PAYMENT_CONFIRMED"):
        return "ADMIN_APPROVAL"
    return None


def should_release_orphan_offer(
    status: str,
    has_live_deal: bool,
    has_live_negotiation: bool,
    status_changed_at: Optional[datetime],
    now: Optional[datetime] = None
) -> bool:
    """
    Pure validator that determines if a 'reserved' or 'negotiating' offer has become
    an orphan (no active Deal and no active Negotiation) and 10 minutes have elapsed since last status change.
    """
    if status not in ("reserved", "negotiating"):
        return False
    if has_live_deal or has_live_negotiation:
        return False
    if status_changed_at is None:
        return False
    current_time = now or datetime.now(timezone.utc)
    if status_changed_at.tzinfo is None:
        status_changed_at = status_changed_at.replace(tzinfo=timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)

    elapsed_seconds = (current_time - status_changed_at).total_seconds()
    return elapsed_seconds >= 600


def can_user_interact_with_offer(offer_user_id: int, user_id: int) -> Tuple[bool, str]:
    """
    Validates that a user cannot trade or negotiate on their own offer.
    Returns (can_interact, reason_ar).
    """
    if offer_user_id == user_id:
        return False, "عذراً، لا يمكنك إجراء صفقة أو تفاوض على عرض قمت بإنشائه بنفسك."
    return True, ""



