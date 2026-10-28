"""Tests for pipeline/utils/ocr.py — confidence scoring and record preprocessing."""

import pytest
from pipeline.utils.ocr import _estimate_confidence, ocr_records, OCR_CONFIDENCE_MIN


class TestEstimateConfidence:
    def test_empty_string_returns_zero(self):
        assert _estimate_confidence("") == 0.0

    def test_whitespace_only_returns_zero(self):
        assert _estimate_confidence("   \n\t  ") == 0.0

    def test_pure_noise_below_threshold(self):
        # Mostly symbols and digits, no real words
        assert _estimate_confidence("1234 5678 @#% ^& 9999 !!??") < OCR_CONFIDENCE_MIN

    def test_normal_french_prose_above_threshold(self):
        text = "Le technicien procède à la vérification de la presse hydraulique."
        assert _estimate_confidence(text) >= OCR_CONFIDENCE_MIN

    def test_python_code_above_threshold(self):
        code = "def process(records):\n    return [r for r in records if r.get('valid')]\n"
        assert _estimate_confidence(code) >= OCR_CONFIDENCE_MIN

    def test_score_in_range(self):
        texts = [
            "some text here",
            "def f(): pass",
            "!!!###@@@",
            "Le rapport annuel de maintenance",
        ]
        for t in texts:
            s = _estimate_confidence(t)
            assert 0.0 <= s <= 1.0, f"Score {s} out of range for: {t!r}"

    def test_longer_good_text_high_confidence(self):
        text = " ".join(["maintenance"] * 50)
        assert _estimate_confidence(text) >= 0.70


class TestOcrRecords:
    def test_records_without_file_path_pass_through(self):
        records = [
            {"text": "some content", "adapter_name": "test"},
            {"code": "def f(): pass", "lang": "python"},
        ]
        kept, excluded = ocr_records(records)
        assert len(kept) == 2
        assert len(excluded) == 0

    def test_record_with_nonexistent_file_is_excluded(self, tmp_path):
        records = [{"file_path": str(tmp_path / "nonexistent.pdf"), "adapter_name": "test"}]
        kept, excluded = ocr_records(records)
        assert len(kept) == 0
        assert len(excluded) == 1
        assert excluded[0]["_ocr_method"] == "file_not_found"

    def test_mixed_records(self, tmp_path):
        # One record with no file_path (should pass), one with missing file (excluded)
        records = [
            {"text": "valid content", "adapter_name": "a"},
            {"file_path": str(tmp_path / "ghost.pdf")},
        ]
        kept, excluded = ocr_records(records)
        assert len(kept) == 1
        assert kept[0]["text"] == "valid content"
        assert len(excluded) == 1

    def test_custom_min_confidence(self, tmp_path):
        # No records with file_path → both pass regardless of threshold
        records = [{"text": "x"}, {"text": "y"}]
        kept, excluded = ocr_records(records, min_confidence=0.99)
        assert len(kept) == 2
        assert len(excluded) == 0
