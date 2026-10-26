import unittest
from bot.utils.support_texts import (
    TERMS_AND_CONDITIONS_TEXT,
    SUPPORT_FAQ_TEXT,
    split_terms_sections,
    reassemble_terms,
    split_faq_items,
    reassemble_faq,
)


class TestSupportSections(unittest.TestCase):
    def test_terms_roundtrip_exact(self):
        preamble, sections = split_terms_sections(TERMS_AND_CONDITIONS_TEXT)
        self.assertTrue(len(sections) >= 8)
        # Check numbering sequence (as per original text: 1 to 10 with 8 skipped)
        section_nums = [int(key) for key, _, _ in sections]
        self.assertEqual(section_nums, list(range(1, len(sections) + 1)))

        # Exact reassembly check
        reassembled = reassemble_terms(preamble, sections)
        self.assertEqual(reassembled, TERMS_AND_CONDITIONS_TEXT)

    def test_faq_roundtrip_exact(self):
        intro, items = split_faq_items(SUPPORT_FAQ_TEXT)
        self.assertTrue(len(items) >= 4)
        for key, q_title, full_block in items:
            self.assertTrue(len(q_title) > 0)
            self.assertTrue(len(full_block) > 0)

        # Exact reassembly check
        reassembled = reassemble_faq(intro, items)
        self.assertEqual(reassembled, SUPPORT_FAQ_TEXT)


if __name__ == "__main__":
    unittest.main()
