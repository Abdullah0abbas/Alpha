from aiogram import Router

from bot.handlers.deals.creation import creation_router, derive_escrow_address
from bot.handlers.deals.lifecycle import lifecycle_router, my_deals
from bot.handlers.deals.payment_info import (
    payment_info_router,
    apply_seller_payment_info,
    forward_proof_photo_to_seller,
    apply_buyer_payout_wallet,
)
from bot.handlers.deals.settlement import settlement_router

deals_router = Router(name="deals")
deals_router.include_router(creation_router)
deals_router.include_router(lifecycle_router)
deals_router.include_router(payment_info_router)
deals_router.include_router(settlement_router)

__all__ = [
    "deals_router",
    "my_deals",
    "derive_escrow_address",
    "apply_seller_payment_info",
    "forward_proof_photo_to_seller",
    "apply_buyer_payout_wallet",
]
