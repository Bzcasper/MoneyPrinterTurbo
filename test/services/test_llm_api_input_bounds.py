import sys
import unittest
from pathlib import Path
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.models.schema import (
    TaskVideoRequest,
    VideoParams,
    VideoScriptParams,
    VideoScriptRequest,
    VideoTermsRequest,
)


class TestLLMAPIInputBounds(unittest.TestCase):
    def test_direct_cli_video_params_unconstrained(self):
        """CLI VideoParams remains unconstrained for subject, script, and terms."""
        huge_subject = "A" * 1000
        huge_script = "B" * 15000
        huge_terms = ["term" * 100] * 100
        params = VideoParams(
            video_subject=huge_subject,
            video_script=huge_script,
            video_terms=huge_terms,
        )
        self.assertEqual(params.video_subject, huge_subject)
        self.assertEqual(params.video_script, huge_script)
        self.assertEqual(params.video_terms, huge_terms)

    def test_task_video_request_subject_bounds(self):
        """TaskVideoRequest limits video_subject to max 500 characters."""
        # Exact boundary: 500 characters valid
        valid_subject = "海" * 500
        req = TaskVideoRequest(video_subject=valid_subject)
        self.assertEqual(req.video_subject, valid_subject)

        # Oversized: 501 characters invalid
        invalid_subject = "海" * 501
        with self.assertRaises(ValidationError):
            TaskVideoRequest(video_subject=invalid_subject)

    def test_task_video_request_script_bounds(self):
        """TaskVideoRequest limits video_script to max 8000 characters."""
        # Exact boundary: 8000 characters valid
        valid_script = "文" * 8000
        req = TaskVideoRequest(video_subject="test", video_script=valid_script)
        self.assertEqual(req.video_script, valid_script)

        # Oversized: 8001 characters invalid
        invalid_script = "文" * 8001
        with self.assertRaises(ValidationError):
            TaskVideoRequest(video_subject="test", video_script=invalid_script)

    def test_task_video_request_terms_bounds(self):
        """TaskVideoRequest limits video_terms count (max 50) and term length (max 255) / string length (max 8000)."""
        # Valid list of terms
        valid_terms = ["词" * 255] * 50
        req = TaskVideoRequest(video_subject="test", video_terms=valid_terms)
        self.assertEqual(req.video_terms, valid_terms)

        # Oversized item in list: item 256 chars
        invalid_item_terms = ["词" * 256]
        with self.assertRaises(ValidationError):
            TaskVideoRequest(video_subject="test", video_terms=invalid_item_terms)

        # Oversized list length: 51 items
        invalid_list_terms = ["词"] * 51
        with self.assertRaises(ValidationError):
            TaskVideoRequest(video_subject="test", video_terms=invalid_list_terms)

        # String terms: valid max 8000 chars
        valid_str_terms = "词," * 2000
        req_str = TaskVideoRequest(video_subject="test", video_terms=valid_str_terms)
        self.assertEqual(req_str.video_terms, valid_str_terms)

        # String terms: invalid 8001 chars
        invalid_str_terms = "词" * 8001
        with self.assertRaises(ValidationError):
            TaskVideoRequest(video_subject="test", video_terms=invalid_str_terms)

    def test_video_script_request_subject_bounds(self):
        """VideoScriptRequest limits video_subject to max 500 characters while VideoScriptParams defaults remain unchanged."""
        # Direct VideoScriptParams can still hold unconstrained subject if needed
        unconstrained = VideoScriptParams()
        unconstrained.video_subject = "A" * 1000
        self.assertEqual(len(unconstrained.video_subject), 1000)

        # Exact boundary: 500 characters valid
        valid_subject = "花" * 500
        req = VideoScriptRequest(video_subject=valid_subject)
        self.assertEqual(req.video_subject, valid_subject)

        # Oversized: 501 characters invalid
        with self.assertRaises(ValidationError):
            VideoScriptRequest(video_subject="花" * 501)

    def test_video_terms_request_subject_and_script_bounds(self):
        """VideoTermsRequest limits video_subject (max 500) and video_script (max 8000)."""
        # Exact boundary
        valid_subject = "海" * 500
        valid_script = "字" * 8000
        req = VideoTermsRequest(video_subject=valid_subject, video_script=valid_script)
        self.assertEqual(req.video_subject, valid_subject)
        self.assertEqual(req.video_script, valid_script)

        # Oversized subject
        with self.assertRaises(ValidationError):
            VideoTermsRequest(video_subject="海" * 501, video_script="test")

        # Oversized script
        with self.assertRaises(ValidationError):
            VideoTermsRequest(video_subject="test", video_script="字" * 8001)


if __name__ == "__main__":
    unittest.main()
