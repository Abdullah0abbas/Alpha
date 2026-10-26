import sys
from unittest.mock import MagicMock

class MockModule(MagicMock):
    __path__ = []

if 'aiogram' not in sys.modules:
    try:
        import aiogram
    except ImportError:
        aiogram_mock = MockModule()
        aiogram_types = MockModule()
        aiogram_types.InlineKeyboardButton = lambda text, callback_data: MagicMock(text=text, callback_data=callback_data)
        aiogram_types.InlineKeyboardMarkup = lambda inline_keyboard: MagicMock(inline_keyboard=inline_keyboard)
        aiogram_exceptions = MockModule()
        aiogram_exceptions.TelegramBadRequest = Exception
        aiogram_exceptions.TelegramForbiddenError = Exception

        sys.modules['aiogram'] = aiogram_mock
        sys.modules['aiogram.types'] = aiogram_types
        sys.modules['aiogram.exceptions'] = aiogram_exceptions
        sys.modules['aiogram.utils'] = MockModule()
        sys.modules['aiogram.utils.keyboard'] = MockModule()

for mod_name in ['sqlalchemy', 'sqlalchemy.ext', 'sqlalchemy.ext.asyncio', 'sqlalchemy.future', 'config', 'config.settings', 'db', 'db.models', 'db.session']:
    if mod_name not in sys.modules:
        try:
            __import__(mod_name)
        except ImportError:
            sys.modules[mod_name] = MockModule()

import unittest
from decimal import Decimal
from datetime import datetime, timezone, timedelta

from bot.utils.deal_card import (
    step_of,
    turn_of,
    get_deal_action_keyboard,
    render_deal_card
)


class MockDeal:
    def __init__(
        self,
        id=1,
        seller_id=1001,
        buyer_id=2002,
        amount_usdt=Decimal("100.0"),
        amount_syp=Decimal("1500000"),
        fee_usdt=Decimal("1.0"),
        deposit_address="0xTestDepositAddr",
        deposit_index=1,
        buyer_payout_address="0xBuyerTestAddr",
        seller_payment_info=None,
        status="PENDING_OWNER_APPROVAL",
        expires_at=None,
        offer_id=10
    ):
        self.id = id
        self.seller_id = seller_id
        self.buyer_id = buyer_id
        self.amount_usdt = amount_usdt
        self.amount_syp = amount_syp
        self.fee_usdt = fee_usdt
        self.deposit_address = deposit_address
        self.deposit_index = deposit_index
        self.buyer_payout_address = buyer_payout_address
        self.seller_payment_info = seller_payment_info or {}
        self.status = status
        self.expires_at = expires_at
        self.offer_id = offer_id


class TestDealCardPureFunctions(unittest.TestCase):

    def test_step_of_mapping(self):
        step, bar, label = step_of("PENDING_OWNER_APPROVAL")
        self.assertEqual(step, 1)
        self.assertIn("موافقة صاحب العرض", label)

        step, bar, label = step_of("CREATED")
        self.assertEqual(step, 2)
        step, bar, label = step_of("AWAITING_USDT_DEPOSIT")
        self.assertEqual(step, 2)
        self.assertIn("إيداع رصيد الضمان", label)

        step, bar, label = step_of("USDT_DEPOSITED")
        self.assertEqual(step, 3)

        step, bar, label = step_of("AWAITING_SYP_PAYMENT")
        self.assertEqual(step, 4)

        step, bar, label = step_of("SYP_PAYMENT_SENT")
        self.assertEqual(step, 5)

        step, bar, label = step_of("SYP_PAYMENT_CONFIRMED")
        self.assertEqual(step, 6)
        step, bar, label = step_of("ADMIN_APPROVAL")
        self.assertEqual(step, 6)

        step, bar, label = step_of("COMPLETED")
        self.assertEqual(step, 7)

        step, bar, label = step_of("CANCELLED")
        self.assertEqual(step, 0)
        self.assertIn("❌", bar)

        step, bar, label = step_of("EXPIRED")
        self.assertEqual(step, 0)
        self.assertIn("⌛", bar)

    def test_turn_of_logic(self):
        is_turn, _ = turn_of("PENDING_OWNER_APPROVAL", viewer_role="buyer", is_offer_owner=True)
        self.assertTrue(is_turn)
        is_turn, _ = turn_of("PENDING_OWNER_APPROVAL", viewer_role="seller", is_offer_owner=False)
        self.assertFalse(is_turn)

        is_turn, _ = turn_of("AWAITING_USDT_DEPOSIT", viewer_role="seller")
        self.assertTrue(is_turn)
        is_turn, _ = turn_of("AWAITING_USDT_DEPOSIT", viewer_role="buyer")
        self.assertFalse(is_turn)

        is_turn, _ = turn_of("USDT_DEPOSITED", viewer_role="seller")
        self.assertTrue(is_turn)
        is_turn, _ = turn_of("USDT_DEPOSITED", viewer_role="buyer")
        self.assertFalse(is_turn)

        is_turn, _ = turn_of("AWAITING_SYP_PAYMENT", viewer_role="buyer")
        self.assertTrue(is_turn)
        is_turn, _ = turn_of("AWAITING_SYP_PAYMENT", viewer_role="seller")
        self.assertFalse(is_turn)

        is_turn, _ = turn_of("SYP_PAYMENT_SENT", viewer_role="seller")
        self.assertTrue(is_turn)
        is_turn, _ = turn_of("SYP_PAYMENT_SENT", viewer_role="buyer")
        self.assertFalse(is_turn)

        is_turn, _ = turn_of("ADMIN_APPROVAL", viewer_role="admin")
        self.assertTrue(is_turn)
        is_turn, _ = turn_of("ADMIN_APPROVAL", viewer_role="buyer")
        self.assertFalse(is_turn)
        is_turn, _ = turn_of("ADMIN_APPROVAL", viewer_role="seller")
        self.assertFalse(is_turn)

        is_turn, _ = turn_of("COMPLETED", viewer_role="seller")
        self.assertFalse(is_turn)
        is_turn, _ = turn_of("CANCELLED", viewer_role="buyer")
        self.assertFalse(is_turn)

    def test_get_deal_action_keyboard(self):
        deal = MockDeal(
            id=42,
            seller_id=1001,
            buyer_id=2002,
            amount_usdt=Decimal("50.0"),
            amount_syp=Decimal("750000"),
            fee_usdt=Decimal("1.0"),
            deposit_address="0xEscrowDepositAddress",
            deposit_index=1,
            buyer_payout_address="0xBuyerAddress",
            seller_payment_info={"info": "0932063758", "method": "syriatel"},
            status="PENDING_OWNER_APPROVAL"
        )

        kb_owner = get_deal_action_keyboard(deal, viewer_id=1001, is_offer_owner=True)
        self.assertIsNotNone(kb_owner)
        callbacks = [btn.callback_data for row in kb_owner.inline_keyboard for btn in row]
        self.assertIn("appr_deal_42", callbacks)
        self.assertIn("rejc_deal_42", callbacks)

        kb_taker = get_deal_action_keyboard(deal, viewer_id=2002, is_offer_owner=False)
        self.assertIsNotNone(kb_taker)
        callbacks_taker = [btn.callback_data for row in kb_taker.inline_keyboard for btn in row]
        self.assertIn("ask_cancel_42", callbacks_taker)

        deal.status = "AWAITING_USDT_DEPOSIT"
        kb_seller = get_deal_action_keyboard(deal, viewer_id=1001)
        self.assertIsNotNone(kb_seller)
        callbacks_seller = [btn.callback_data for row in kb_seller.inline_keyboard for btn in row]
        self.assertIn("man_dep_42", callbacks_seller)
        self.assertIn("ask_cancel_42", callbacks_seller)

        deal.status = "SYP_PAYMENT_SENT"
        kb_seller_conf = get_deal_action_keyboard(deal, viewer_id=1001)
        self.assertIsNotNone(kb_seller_conf)
        callbacks_conf = [btn.callback_data for row in kb_seller_conf.inline_keyboard for btn in row]
        self.assertIn("ask_conf_42", callbacks_conf)

        deal.status = "COMPLETED"
        self.assertIsNone(get_deal_action_keyboard(deal, viewer_id=1001))
        self.assertIsNone(get_deal_action_keyboard(deal, viewer_id=2002))

    def test_render_deal_card(self):
        deal = MockDeal(
            id=99,
            seller_id=1001,
            buyer_id=2002,
            amount_usdt=Decimal("100.0"),
            amount_syp=Decimal("1500000"),
            fee_usdt=Decimal("1.0"),
            deposit_address="0xTestDepositAddr",
            deposit_index=2,
            buyer_payout_address="0xBuyerTestAddr",
            seller_payment_info={"info": "0911223344", "method": "syriatel"},
            status="AWAITING_SYP_PAYMENT",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15)
        )

        buyer_text, buyer_kb = render_deal_card(deal, viewer_id=2002)
        self.assertIn("بطاقة صفقة الضمان #99", buyer_text)
        self.assertIn("100", buyer_text)
        self.assertIn("1,500,000", buyer_text)
        self.assertIn("الخطوة 4 من 7", buyer_text)
        self.assertIn("دورك الآن", buyer_text)
        self.assertIsNotNone(buyer_kb)
        callbacks = [btn.callback_data for row in buyer_kb.inline_keyboard for btn in row]
        self.assertIn("man_syp_99", callbacks)

        seller_text, seller_kb = render_deal_card(deal, viewer_id=1001)
        self.assertIn("بطاقة صفقة الضمان #99", seller_text)
        self.assertIn("بانتظار قيام المشتري بتحويل المبلغ بالليرة", seller_text)
        self.assertIsNone(seller_kb)


if __name__ == "__main__":
    unittest.main()
