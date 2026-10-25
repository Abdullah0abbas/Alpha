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


if __name__ == "__main__":
    unittest.main()

