import unittest
from bot.utils.validators import validate_bep20_address


class TestValidators(unittest.TestCase):
    def test_1_valid_lowercase(self):
        addr = "0x55d398326f99059ff775485246999027b3197955"
        ok, norm, err = validate_bep20_address(addr)
        self.assertTrue(ok)
        self.assertEqual(norm, "0x55d398326f99059fF775485246999027B3197955")
        self.assertEqual(err, "")

    def test_2_valid_checksum(self):
        addr = "0x55d398326f99059fF775485246999027B3197955"
        ok, norm, err = validate_bep20_address(addr)
        self.assertTrue(ok)
        self.assertEqual(norm, addr)
        self.assertEqual(err, "")

    def test_3_invalid_checksum(self):
        # Alter capitalisation to break EIP-55 checksum
        addr = "0x55D398326f99059fF775485246999027B3197955"
        ok, norm, err = validate_bep20_address(addr)
        self.assertFalse(ok)
        self.assertEqual(norm, "")
        self.assertIn("checksum", err.lower())

    def test_4_short_length(self):
        addr = "0x55d398326f99059ff"
        ok, norm, err = validate_bep20_address(addr)
        self.assertFalse(ok)
        self.assertEqual(norm, "")
        self.assertTrue(len(err) > 0)

    def test_5_long_length(self):
        addr = "0x55d398326f99059ff775485246999027b31979559999"
        ok, norm, err = validate_bep20_address(addr)
        self.assertFalse(ok)
        self.assertEqual(norm, "")
        self.assertTrue(len(err) > 0)

    def test_6_trc20_address(self):
        addr = "TLa2f6VPqDgRE67v1736s7bJ8Ray5wYjU7"
        ok, norm, err = validate_bep20_address(addr)
        self.assertFalse(ok)
        self.assertEqual(norm, "")
        self.assertTrue(len(err) > 0)

    def test_7_internal_space(self):
        addr = "0x55d39832 6f99059ff775485246999027b3197955"
        ok, norm, err = validate_bep20_address(addr)
        self.assertFalse(ok)
        self.assertEqual(norm, "")
        self.assertTrue(len(err) > 0)

    def test_8_rtl_and_invisible_chars(self):
        addr = "\u200f0x55d398326f99059ff775485246999027b3197955\u200e"
        ok, norm, err = validate_bep20_address(addr)
        self.assertTrue(ok)
        self.assertEqual(norm, "0x55d398326f99059fF775485246999027B3197955")
        self.assertEqual(err, "")

    def test_9_zero_address(self):
        addr = "0x0000000000000000000000000000000000000000"
        ok, norm, err = validate_bep20_address(addr)
        self.assertFalse(ok)
        self.assertEqual(norm, "")
        self.assertIn("محظور", err)

    def test_10_dead_address(self):
        addr = "0x000000000000000000000000000000000000dead"
        ok, norm, err = validate_bep20_address(addr)
        self.assertFalse(ok)
        self.assertEqual(norm, "")
        self.assertIn("محظور", err)

    def test_11_empty_and_none(self):
        ok, norm, err = validate_bep20_address("")
        self.assertFalse(ok)
        self.assertIn("فارغ", err)

        ok, norm, err = validate_bep20_address(None)
        self.assertFalse(ok)
        self.assertIn("فارغ", err)

    def test_sham_cash_validator(self):
        from bot.utils.validators import validate_sham_cash_address
        valid_addr = "82b2a4fafc335c4f39c58910248c63d1"
        ok, norm, err = validate_sham_cash_address(valid_addr)
        self.assertTrue(ok)
        self.assertEqual(norm, valid_addr)
        self.assertEqual(err, "")

        # Uppercase
        ok, norm, _ = validate_sham_cash_address(valid_addr.upper())
        self.assertTrue(ok)
        self.assertEqual(norm, valid_addr)

        # Invalid lengths and characters
        self.assertFalse(validate_sham_cash_address("82b2a4fafc")[0])
        self.assertFalse(validate_sham_cash_address("82b2a4fafc335c4f39c58910248c63d1extra")[0])
        self.assertFalse(validate_sham_cash_address("zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz")[0])
        self.assertFalse(validate_sham_cash_address("")[0])

    def test_syriatel_cash_validator(self):
        from bot.utils.validators import validate_syriatel_cash_number
        phone = "0932063758"
        ok, norm, err = validate_syriatel_cash_number(phone)
        self.assertTrue(ok)
        self.assertEqual(norm, phone)

        # With international prefixes and spaces
        ok, norm, _ = validate_syriatel_cash_number("+963 932 063 758")
        self.assertTrue(ok)
        self.assertEqual(norm, phone)

        ok, norm, _ = validate_syriatel_cash_number("00963932063758")
        self.assertTrue(ok)
        self.assertEqual(norm, phone)

        # Invalid
        self.assertFalse(validate_syriatel_cash_number("0932063")[0])
        self.assertFalse(validate_syriatel_cash_number("0832063758")[0])
        self.assertFalse(validate_syriatel_cash_number("093206375899")[0])

    def test_bank_transfer_validator(self):
        from bot.utils.validators import validate_bank_transfer_info
        raw = "محمد أحمد العلي - 0932063758"
        ok, norm, err = validate_bank_transfer_info(raw)
        self.assertTrue(ok)
        self.assertIn("محمد أحمد العلي", norm)
        self.assertIn("0932063758", norm)

        # Missing phone
        self.assertFalse(validate_bank_transfer_info("محمد أحمد العلي")[0])
        # Missing triple name (only 1 or 2 names)
        self.assertFalse(validate_bank_transfer_info("محمد 0932063758")[0])
        self.assertFalse(validate_bank_transfer_info("محمد أحمد 0932063758")[0])

    def test_unified_payment_method_info(self):
        from bot.utils.validators import validate_payment_method_info
        # Sham cash
        ok, norm, _ = validate_payment_method_info("شام كاش ليرة", "82b2a4fafc335c4f39c58910248c63d1")
        self.assertTrue(ok)
        self.assertIn("82b2a4fafc335c4f39c58910248c63d1", norm)

        # Syriatel cash
        ok, norm, _ = validate_payment_method_info("سيريتيل كاش", "0932063758")
        self.assertTrue(ok)
        self.assertIn("0932063758", norm)

        # Bank transfer
        ok, norm, _ = validate_payment_method_info("حوالة بنكية (الهرم)", "أحمد محمود حسن 0932063758")
        self.assertTrue(ok)
        self.assertIn("أحمد محمود حسن", norm)


class TestOfferWizardPureFunctions(unittest.TestCase):
    def test_wizard_step_push_and_pop(self):
        from bot.utils.offer_wizard import push_wizard_step, pop_wizard_step

        history = []
        history = push_wizard_step(history, "type", {"offer_type": "sell"})
        history = push_wizard_step(history, "rate", {"rate": "15000"})
        self.assertEqual(len(history), 2)

        prev, new_hist = pop_wizard_step(history)
        self.assertIsNotNone(prev)
        self.assertEqual(prev["step"], "rate")
        self.assertEqual(prev["data"]["rate"], "15000")
        self.assertEqual(len(new_hist), 1)

        prev2, new_hist2 = pop_wizard_step(new_hist)
        self.assertEqual(prev2["step"], "type")
        self.assertEqual(len(new_hist2), 0)

        empty_prev, empty_hist = pop_wizard_step([])
        self.assertIsNone(empty_prev)
        self.assertEqual(empty_hist, [])

    def test_extract_primary_payout_info(self):
        from bot.utils.offer_wizard import extract_primary_payout_info

        # Buy offer returns BEP-20 address
        buy_data = {
            "offer_type": "buy",
            "payout_details": {
                "usdt_bep20_address": "0x55d398326f99059fF775485246999027B3197955"
            }
        }
        self.assertEqual(
            extract_primary_payout_info(buy_data),
            "0x55d398326f99059fF775485246999027B3197955"
        )

        # Sell offer returns first matching payment method account
        sell_data = {
            "offer_type": "sell",
            "payment_methods": ["سيريتيل كاش", "شام كاش ليرة"],
            "payout_details": {
                "سيريتيل كاش": "سيريتيل كاش: 0932063758",
                "شام كاش ليرة": "شام كاش: 82b2a4fafc335c4f39c58910248c63d1"
            }
        }
        self.assertEqual(
            extract_primary_payout_info(sell_data),
            "سيريتيل كاش: 0932063758"
        )

    def test_is_wizard_data_complete(self):
        from bot.utils.offer_wizard import is_wizard_data_complete

        # Complete valid sell offer
        valid_sell = {
            "offer_type": "sell",
            "rate": "15000",
            "min_limit": "50",
            "max_limit": "200",
            "payment_methods": ["سيريتيل كاش"],
            "payout_details": {
                "سيريتيل كاش": "0932063758"
            },
            "duration_hours": 4.0
        }
        ok, err = is_wizard_data_complete(valid_sell)
        self.assertTrue(ok)
        self.assertEqual(err, "")

        # Incomplete or invalid: max < min
        invalid_limits = dict(valid_sell, min_limit="300", max_limit="100")
        ok, err = is_wizard_data_complete(invalid_limits)
        self.assertFalse(ok)
        self.assertIn("الحد الأقصى", err)

        # Incomplete: missing payout details for sell
        missing_payout = dict(valid_sell, payout_details={})
        ok, err = is_wizard_data_complete(missing_payout)
        self.assertFalse(ok)
        self.assertIn("بيانات حساب الاستلام", err)

        # Sham Cash USD rejection
        with_sham_usd = dict(valid_sell, payment_methods=["شام كاش دولار"])
        ok, err = is_wizard_data_complete(with_sham_usd)
        self.assertFalse(ok)
        self.assertIn("شام كاش دولار", err)

        # Valid buy offer
        valid_buy = {
            "offer_type": "buy",
            "rate": "14800",
            "min_limit": "20",
            "max_limit": "500",
            "payment_methods": ["سيريتيل كاش"],
            "payout_details": {
                "usdt_bep20_address": "0x55d398326f99059fF775485246999027B3197955"
            },
            "duration_hours": 3.0
        }
        ok, err = is_wizard_data_complete(valid_buy)
        self.assertTrue(ok)
        self.assertEqual(err, "")

        # Invalid BEP-20 address in buy offer
        invalid_wallet_buy = dict(valid_buy, payout_details={"usdt_bep20_address": "0xinvalid"})
        ok, err = is_wizard_data_complete(invalid_wallet_buy)
        self.assertFalse(ok)
        self.assertIn("محفظة", err)

    def test_format_wizard_summary(self):
        from bot.utils.offer_wizard import format_wizard_summary

        data = {
            "offer_type": "sell",
            "rate": "15200",
            "min_limit": "50",
            "max_limit": "500",
            "payment_methods": ["سيريتيل كاش"],
            "payout_details": {
                "سيريتيل كاش": "سيريتيل كاش: 0932063758"
            },
            "negotiable": True,
            "negotiable_type": "amount",
            "duration_hours": 4.0
        }
        summary = format_wizard_summary(data, bot_name="TestBot")
        self.assertIn("ملخص ومراجعة العرض النهائي", summary)
        self.assertIn("15,200", summary)
        self.assertIn("50", summary)
        self.assertIn("500", summary)
        self.assertIn("0932063758", summary)
        self.assertIn("متاح على الكمية", summary)
        self.assertIn("عمولة المنصة", summary)


if __name__ == "__main__":
    unittest.main()


