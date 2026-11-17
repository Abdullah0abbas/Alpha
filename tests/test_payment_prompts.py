import sys
from unittest.mock import MagicMock

if 'aiogram' not in sys.modules:
    try:
        import aiogram  # noqa: F401
    except ImportError:
        class _MockModule(MagicMock):
            __path__ = []

        aiogram_mock = _MockModule()
        aiogram_types = _MockModule()
        aiogram_types.InlineKeyboardButton = lambda text, callback_data: MagicMock(
            text=text, callback_data=callback_data
        )
        aiogram_types.InlineKeyboardMarkup = lambda inline_keyboard: MagicMock(
            inline_keyboard=inline_keyboard
        )
        sys.modules['aiogram'] = aiogram_mock
        sys.modules['aiogram.types'] = aiogram_types

import unittest
from types import SimpleNamespace

from bot.utils.payment_prompts import (
    build_allowed_pm_selection_kb,
    build_deal_pm_prompt,
    build_payment_method_selection_kb,
    build_profile_pm_prompt,
    build_seller_pm_creation_prompt,
    extract_clean_account,
    match_saved_payment_info,
    resolve_deal_target_pm,
    resolve_pm_key,
)


class TestPaymentPrompts(unittest.TestCase):
    def test_resolve_pm_key(self):
        self.assertEqual(resolve_pm_key("شام كاش ليرة"), "sham_lira")
        self.assertEqual(resolve_pm_key("شام كاش"), "sham_lira")
        self.assertEqual(resolve_pm_key("سيريتيل كاش"), "syriatel")
        self.assertEqual(resolve_pm_key("سيريتل كاش"), "syriatel")
        self.assertEqual(resolve_pm_key("حوالة بنكية (الهرم)"), "haram")
        self.assertEqual(resolve_pm_key("حوالة بنكية (الفؤاد)"), "fouad")
        self.assertEqual(resolve_pm_key(None), "sham_lira")

    def test_extract_clean_account(self):
        sham_hex = "a" * 32
        self.assertEqual(
            extract_clean_account(f"شام كاش ليرة: {sham_hex}", "شام كاش ليرة"),
            sham_hex,
        )
        self.assertEqual(
            extract_clean_account("سيريتيل كاش: 0912345678", "سيريتيل كاش"),
            "0912345678",
        )
        self.assertEqual(
            extract_clean_account("حوالة بنكية (الهرم): أحمد محمد علي - 0912345678", "حوالة بنكية (الهرم)"),
            "أحمد محمد علي - 0912345678",
        )
        self.assertEqual(extract_clean_account("PENDING"), "")
        self.assertEqual(extract_clean_account("غير محدد"), "")
        self.assertEqual(extract_clean_account("0912345678", "سيريتيل كاش"), "0912345678")

    def test_match_saved_payment_info_compatibility(self):
        sham_hex = "b" * 32
        ok, method, acc, reason = match_saved_payment_info(
            f"شام كاش ليرة: {sham_hex}",
            target_method="شام كاش ليرة",
        )
        self.assertTrue(ok)
        self.assertEqual(method, "شام كاش ليرة")
        self.assertEqual(acc, sham_hex)
        self.assertEqual(reason, "")

        # Mismatched method against locked deal payment method must be rejected
        ok_bad, method_bad, _, reason_bad = match_saved_payment_info(
            "سيريتيل كاش: 0912345678",
            target_method="شام كاش ليرة",
        )
        self.assertFalse(ok_bad)
        self.assertEqual(method_bad, "سيريتيل كاش")
        self.assertIn("لا يطابق", reason_bad)

        # Allowed methods filtering when deal has no locked method yet
        ok_allowed, method_allowed, acc_allowed, _ = match_saved_payment_info(
            "سيريتيل كاش: 0912345678",
            allowed_methods=["سيريتيل كاش", "حوالة بنكية (الهرم)"],
        )
        self.assertTrue(ok_allowed)
        self.assertEqual(method_allowed, "سيريتيل كاش")
        self.assertEqual(acc_allowed, "0912345678")

        ok_disallowed, _, _, reason_disallowed = match_saved_payment_info(
            "سيريتيل كاش: 0912345678",
            allowed_methods=["حوالة بنكية (الهرم)"],
        )
        self.assertFalse(ok_disallowed)
        self.assertIn("غير ضمن وسائل الدفع المقبولة", reason_disallowed)

        # Legacy raw Sham Cash without label still resolves cleanly
        ok_legacy, m_legacy, acc_legacy, _ = match_saved_payment_info(
            sham_hex,
            target_method="شام كاش ليرة",
        )
        self.assertTrue(ok_legacy)
        self.assertEqual(m_legacy, "شام كاش ليرة")
        self.assertEqual(acc_legacy, sham_hex)

    def test_build_profile_pm_prompt(self):
        res = build_profile_pm_prompt("sham_lira")
        self.assertIsNotNone(res)
        name, text = res
        self.assertEqual(name, "شام كاش ليرة")
        self.assertIn("32 خانة hex", text)

        self.assertIsNone(build_profile_pm_prompt("unknown_pm"))

    def test_build_deal_pm_prompt_strict_and_fallback(self):
        self.assertIsNone(build_deal_pm_prompt("invalid_key", 42, strict=True))

        name, text = build_deal_pm_prompt("سيريتيل كاش", 42)
        self.assertEqual(name, "سيريتيل كاش")
        self.assertIn("صفقة #42", text)
        self.assertIn("09", text)

        bank_name, bank_text = build_deal_pm_prompt("haram", 7, strict=True)
        self.assertEqual(bank_name, "حوالة بنكية (الهرم)")
        self.assertIn("صفقة #7", bank_text)
        self.assertIn("الاسم الثلاثي", bank_text)

    def test_build_seller_pm_creation_prompt(self):
        text = build_seller_pm_creation_prompt("شام كاش ليرة", 99)
        self.assertIn("عرض #99", text)
        self.assertIn("شام كاش ليرة", text)
        self.assertIn("32 خانة hex", text)

        syriatel_text = build_seller_pm_creation_prompt("سيريتيل كاش", None)
        self.assertIn("سيريتيل كاش", syriatel_text)
        self.assertIn("09", syriatel_text)

    def test_build_payment_method_selection_kb(self):
        kb = build_payment_method_selection_kb(
            callback_prefix="deal_pm_",
            back_text="❌ إلغاء",
            back_callback="fsm_cancel_back",
            callback_suffix="_15",
        )
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertEqual(
            callbacks,
            [
                "deal_pm_sham_lira_15",
                "deal_pm_syriatel_15",
                "deal_pm_haram_15",
                "deal_pm_fouad_15",
                "fsm_cancel_back",
            ],
        )

    def test_build_allowed_pm_selection_kb_scopes_to_offer_methods(self):
        kb = build_allowed_pm_selection_kb(
            allowed_methods=["سيريتيل كاش", "حوالة بنكية (الفؤاد)"],
            callback_prefix="deal_pm_",
            back_text="❌ إلغاء",
            back_callback="fsm_cancel_back",
            callback_suffix="_88",
        )
        callbacks = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        self.assertEqual(
            callbacks,
            [
                "deal_pm_syriatel_88",
                "deal_pm_fouad_88",
                "fsm_cancel_back",
            ],
        )

    def test_resolve_deal_target_pm(self):
        deal_with_method = SimpleNamespace(seller_payment_info={"method": "سيريتيل كاش", "info": "PENDING"})
        self.assertEqual(resolve_deal_target_pm(deal_with_method, ["شام كاش ليرة"]), "سيريتيل كاش")

        deal_pending = SimpleNamespace(seller_payment_info={"method": "PENDING", "info": "PENDING"})
        self.assertEqual(
            resolve_deal_target_pm(deal_pending, ["حوالة بنكية (الهرم)"]),
            "حوالة بنكية (الهرم)",
        )
        self.assertEqual(resolve_deal_target_pm(deal_pending, []), "شام كاش ليرة")


if __name__ == "__main__":
    unittest.main()
