"""Deal fee calculation utilities.

Pure mathematical and formatting functions for the platform commission model.
Zero dependencies on aiogram or sqlalchemy.
"""
from decimal import Decimal, ROUND_HALF_UP
from typing import NamedTuple

# Commission rule constants
FEE_THRESHOLD_USDT: Decimal = Decimal("25")
FEE_RATE: Decimal = Decimal("0.001")  # 0.1% per counterparty


class DealFeeBreakdown(NamedTuple):
    """Immutable snapshot of calculated deal commission."""
    amount_usdt: Decimal
    seller_fee: Decimal
    buyer_fee: Decimal
    total_fee: Decimal
    seller_deposit_required: Decimal
    buyer_receives: Decimal
    is_free: bool


def calculate_deal_fee(amount: Decimal | str | float | int) -> DealFeeBreakdown:
    """Calculate platform commission for both buyer and seller.

    Rule:
    - If amount <= 25 USDT: fee per side = 0 USDT.
    - If amount > 25 USDT: fee per side = amount * 0.001 (quantized to 6 decimals, ROUND_HALF_UP).
    - Seller deposits: amount + seller_fee.
    - Buyer receives: amount - buyer_fee.
    - Total platform fee: seller_fee + buyer_fee.
    """
    if not isinstance(amount, Decimal):
        amount = Decimal(str(amount))

    if amount <= FEE_THRESHOLD_USDT:
        zero = Decimal("0.000000")
        return DealFeeBreakdown(
            amount_usdt=amount,
            seller_fee=zero,
            buyer_fee=zero,
            total_fee=zero,
            seller_deposit_required=amount,
            buyer_receives=amount,
            is_free=True,
        )

    # 0.1% on the full amount, quantized to 6 decimal places (micro-USDT precision)
    fee_per_side = (amount * FEE_RATE).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    total_fee = (fee_per_side * 2).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
    seller_deposit = amount + fee_per_side
    buyer_net = amount - fee_per_side

    return DealFeeBreakdown(
        amount_usdt=amount,
        seller_fee=fee_per_side,
        buyer_fee=fee_per_side,
        total_fee=total_fee,
        seller_deposit_required=seller_deposit,
        buyer_receives=buyer_net,
        is_free=False,
    )


def format_fee_rule_line() -> str:
    """Return standard single-line platform fee rule description for listings and cards."""
    return f"⚡ العمولة: المبلغ ≤ {FEE_THRESHOLD_USDT} USDT ← 0 | المبلغ > {FEE_THRESHOLD_USDT} USDT ← 1/1000 لكل طرف"


def format_fee_rule_html() -> str:
    """Return HTML-safe platform fee rule description for HTML-formatted messages."""
    return f"⚡ العمولة: المبلغ ≤ {FEE_THRESHOLD_USDT} USDT ← 0 | المبلغ &gt; {FEE_THRESHOLD_USDT} USDT ← 1/1000 لكل طرف"

