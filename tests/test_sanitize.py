import unittest
from bot.utils.sanitize import (
    mask_value,
    mask_details,
    sanitize_exception_message,
)


class TestSanitize(unittest.TestCase):
    def test_mask_value_short(self):
        # Length <= 14 must remain unmodified
        self.assertEqual(mask_value("0x123456"), "0x123456")
        self.assertEqual(mask_value("12345678901234"), "12345678901234")
        self.assertIsNone(mask_value(None))

    def test_mask_value_long(self):
        # Length > 14: first 6, '…', last 4
        addr = "0x71C8471543940172e293043818e5473e"
        masked = mask_value(addr)
        self.assertEqual(masked, "0x71C8…473e")
        self.assertEqual(len(masked), 6 + 1 + 4)

    def test_mask_details_nested_and_keys(self):
        original = {
            "action": "RELEASE",
            "deposit_address": "0x71C8471543940172e293043818e5473e",
            "user_wallet": "TXYZ9876543210987654",
            "payout_info": "Bank Audi 01234567899999",
            "payment_method": "Syriatel Cash 0933111222333",
            "unrelated_key": "Regular public information that is long enough",
            "nested": {
                "seller_address": "0xABCDEF0123456789ABCDEF",
                "normal_flag": True
            }
        }

        masked = mask_details(original)

        # Sensitive keys masked
        self.assertEqual(masked["deposit_address"], "0x71C8…473e")
        self.assertEqual(masked["user_wallet"], "TXYZ98…7654")
        self.assertEqual(masked["payout_info"], "Bank A…9999")
        self.assertEqual(masked["payment_method"], "Syriat…2333")
        # Non-sensitive keys untouched
        self.assertEqual(masked["action"], "RELEASE")
        self.assertEqual(masked["unrelated_key"], "Regular public information that is long enough")
        # Nested keys masked recursively
        self.assertEqual(masked["nested"]["seller_address"], "0xABCD…CDEF")
        self.assertTrue(masked["nested"]["normal_flag"])

    def test_mask_details_does_not_mutate_original(self):
        original = {
            "address": "0x71C8471543940172e293043818e5473e",
            "nested": {
                "wallet": "TXYZ9876543210987654"
            }
        }
        _ = mask_details(original)
        self.assertEqual(original["address"], "0x71C8471543940172e293043818e5473e")
        self.assertEqual(original["nested"]["wallet"], "TXYZ9876543210987654")

    def test_sanitize_exception_message(self):
        # Typical SQLAlchemy error message with SQL and parameters and background info
        raw_exc = (
            "(sqlalchemy.dialects.postgresql.asyncpg.Error) <class 'asyncpg.exceptions.UniqueViolationError'>: "
            "duplicate key value violates unique constraint 'users_pkey'\n"
            "DETAIL: Key (telegram_id)=(12345678) already exists.\n"
            "[SQL: INSERT INTO users (telegram_id, username) VALUES ($1, $2)]\n"
            "[parameters: (12345678, 'secret_user')]\n"
            "(Background on this error at: https://sqlalche.me/e/20/gkpj)"
        )

        sanitized = sanitize_exception_message(raw_exc, max_len=200)

        # Must not contain SQL, parameters, background, or newlines
        self.assertNotIn("[SQL:", sanitized)
        self.assertNotIn("[parameters:", sanitized)
        self.assertNotIn("(Background on this error", sanitized)
        self.assertNotIn("\n", sanitized)
        self.assertLessEqual(len(sanitized), 200)
        self.assertIn("duplicate key value violates unique constraint", sanitized)


if __name__ == "__main__":
    unittest.main()
