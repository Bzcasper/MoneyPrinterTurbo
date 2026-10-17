import unittest
from unittest.mock import patch

import app.services.llm as llm


class TestScriptParagraphCount(unittest.TestCase):
    def test_three_paragraphs_requested_as_two_returns_first_two(self):
        def fake_generate_response(prompt):
            return "Paragraph 1.\n\nParagraph 2.\n\nParagraph 3."

        with patch.object(llm, "_generate_response", side_effect=fake_generate_response):
            result = llm.generate_script(video_subject="test", paragraph_number=2)

        expected = "Paragraph 1.\n\nParagraph 2."
        self.assertEqual(result, expected)

    def test_one_paragraph_requested(self):
        def fake_generate_response(prompt):
            return "Paragraph 1.\n\nParagraph 2.\n\nParagraph 3."

        with patch.object(llm, "_generate_response", side_effect=fake_generate_response):
            result = llm.generate_script(video_subject="test", paragraph_number=1)

        expected = "Paragraph 1."
        self.assertEqual(result, expected)

    def test_fewer_returned_paragraphs_than_requested(self):
        def fake_generate_response(prompt):
            return "Paragraph 1.\n\nParagraph 2."

        with patch.object(llm, "_generate_response", side_effect=fake_generate_response):
            result = llm.generate_script(video_subject="test", paragraph_number=5)

        expected = "Paragraph 1.\n\nParagraph 2."
        self.assertEqual(result, expected)

    def test_blank_lines_and_excess_whitespace(self):
        def fake_generate_response(prompt):
            return "   \n\n  Paragraph 1.   \n\n\n\n  \n\n   Paragraph 2.  \n\n   Paragraph 3.   "

        with patch.object(llm, "_generate_response", side_effect=fake_generate_response):
            result = llm.generate_script(video_subject="test", paragraph_number=2)

        expected = "Paragraph 1.\n\nParagraph 2."
        self.assertEqual(result, expected)

    def test_invalid_count_clamping(self):
        def fake_generate_response(prompt):
            return "\n\n".join([f"Paragraph {i}." for i in range(1, 15)])

        # Test negative/zero clamped to MIN_SCRIPT_PARAGRAPH_NUMBER (1)
        with patch.object(llm, "_generate_response", side_effect=fake_generate_response):
            result_zero = llm.generate_script(video_subject="test", paragraph_number=0)
        self.assertEqual(result_zero, "Paragraph 1.")

        # Test count > MAX_SCRIPT_PARAGRAPH_NUMBER (10) clamped to 10
        with patch.object(llm, "_generate_response", side_effect=fake_generate_response):
            result_15 = llm.generate_script(video_subject="test", paragraph_number=15)
        self.assertEqual(len(result_15.split("\n\n")), 10)
