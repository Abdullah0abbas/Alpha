import unittest
from bot.utils.branding import (
    BOT_NAME,
    CHANNEL_NAME,
    TAGLINE,
    brand_header,
    brand_footer
)


class TestBranding(unittest.TestCase):
    def test_constants_defined(self):
        self.assertTrue(isinstance(BOT_NAME, str) and len(BOT_NAME) > 0)
        self.assertTrue(isinstance(CHANNEL_NAME, str) and len(CHANNEL_NAME) > 0)
        self.assertTrue(isinstance(TAGLINE, str) and len(TAGLINE) > 0)
        self.assertEqual(BOT_NAME, "Alpha Bot")
        self.assertEqual(CHANNEL_NAME, "Alpha P2P")

    def test_brand_header_default(self):
        header = brand_header()
        self.assertTrue(len(header) > 0)
        self.assertTrue(header.startswith("\u200f"))
        # Check markdown balance for asterisks
        self.assertEqual(header.count("*") % 2, 0)
        self.assertEqual(header.count("`") % 2, 0)
        self.assertEqual(header.count("_") % 2, 0)

    def test_brand_header_with_title(self):
        header = brand_header("لوحة التحكم")
        self.assertIn("لوحة التحكم", header)
        self.assertTrue(header.startswith("\u200f"))
        self.assertEqual(header.count("*") % 2, 0)
        self.assertEqual(header.count("`") % 2, 0)
        self.assertEqual(header.count("_") % 2, 0)

    def test_brand_footer(self):
        footer = brand_footer()
        self.assertTrue(len(footer) > 0)
        self.assertTrue(footer.startswith("\u200f"))
        self.assertIn(BOT_NAME, footer)
        self.assertEqual(footer.count("*") % 2, 0)
        self.assertEqual(footer.count("`") % 2, 0)
        self.assertEqual(footer.count("_") % 2, 0)


if __name__ == "__main__":
    unittest.main()
