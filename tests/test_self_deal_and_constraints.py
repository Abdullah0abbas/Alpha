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

HAS_REAL_DB_MODELS = False
try:
    import db.models
    import sqlalchemy
    if hasattr(sqlalchemy, 'CheckConstraint') and not isinstance(sqlalchemy.CheckConstraint, MagicMock):
        from db.models import Deal, Negotiation
        HAS_REAL_DB_MODELS = True
except Exception:
    HAS_REAL_DB_MODELS = False

import unittest

from bot.utils.transitions import can_user_interact_with_offer
from bot.utils.deal_views import build_offer_action_buttons


class TestSelfDealAndConstraints(unittest.TestCase):
    def test_self_deal_prevention_validator(self):
        """Ensures that user cannot trade or negotiate on their own offer."""
        offer_user_id = 998877
        taker_user_id = 998877
        allowed, msg = can_user_interact_with_offer(offer_user_id, taker_user_id)
        self.assertFalse(allowed)
        self.assertIn("بنفسك", msg)

        different_taker_id = 112233
        allowed, msg = can_user_interact_with_offer(offer_user_id, different_taker_id)
        self.assertTrue(allowed)

    def test_model_table_args_constraints(self):
        """Verifies that Deal and Negotiation models define the required DB constraints if real models available."""
        if not HAS_REAL_DB_MODELS:
            self.skipTest("Real db.models with sqlalchemy not available in current test environment")

        deal_table_args = getattr(Deal, '__table_args__', ())
        deal_constraint_names = [
            getattr(arg, 'name', None) for arg in deal_table_args if hasattr(arg, 'name')
        ]
        self.assertIn("chk_deals_different_parties", deal_constraint_names)
        self.assertIn("uq_open_deal_per_offer", deal_constraint_names)

        neg_table_args = getattr(Negotiation, '__table_args__', ())
        neg_constraint_names = [
            getattr(arg, 'name', None) for arg in neg_table_args if hasattr(arg, 'name')
        ]
        self.assertIn("uq_open_negotiation_per_offer", neg_constraint_names)

    def test_button_filtering_for_offer_owner(self):
        """
        Tests build_offer_action_buttons directly across offers, browse, and start contexts.
        Ensures action buttons are omitted when viewer is offer creator.
        """
        offer_id = 42
        creator_id = 7001
        buyer_id = 7002

        # 1. Offers context
        creator_offers_btns = build_offer_action_buttons(offer_id, creator_id, creator_id, True, "both", "offers")
        self.assertEqual(len(creator_offers_btns), 0)

        buyer_offers_btns = build_offer_action_buttons(offer_id, creator_id, buyer_id, True, "both", "offers")
        self.assertEqual(len(buyer_offers_btns), 2)
        self.assertIn(f"start_deal_{offer_id}", buyer_offers_btns[0][0].callback_data)
        self.assertIn(f"start_neg_{offer_id}", buyer_offers_btns[1][0].callback_data)

        # 2. Browse context
        creator_browse_btns = build_offer_action_buttons(offer_id, creator_id, creator_id, True, "both", "browse")
        self.assertEqual(len(creator_browse_btns), 0)

        buyer_browse_btns = build_offer_action_buttons(offer_id, creator_id, buyer_id, True, "both", "browse")
        self.assertEqual(len(buyer_browse_btns), 1)
        self.assertEqual(len(buyer_browse_btns[0]), 2)

        # 3. Start context
        creator_start_btns = build_offer_action_buttons(offer_id, creator_id, creator_id, True, "both", "start")
        self.assertEqual(len(creator_start_btns), 1)
        self.assertEqual(creator_start_btns[0][0].callback_data, "menu_back_main")

        buyer_start_btns = build_offer_action_buttons(offer_id, creator_id, buyer_id, True, "both", "start")
        self.assertEqual(len(buyer_start_btns), 3)


if __name__ == "__main__":
    unittest.main()
