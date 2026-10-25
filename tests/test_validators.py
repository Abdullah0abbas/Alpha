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


if __name__ == "__main__":
    unittest.main()
