"""Tests for pipeline/utils/metrics.py."""

from pipeline.utils.metrics import rouge_l_score, token_length_stats
from pipeline.utils.jsonl import write_jsonl


# ── rouge_l_score ─────────────────────────────────────────────────────────────

class TestRougeL:
    def test_identical_strings_max_score(self) -> None:
        text = "le contrat est résilié avec un préavis de trois mois"
        assert rouge_l_score(text, text) == 1.0

    def test_unrelated_strings_low_score(self) -> None:
        assert rouge_l_score("alpha beta gamma", "one two three") < 0.1

    def test_empty_input_scores_zero(self) -> None:
        assert rouge_l_score("", "reference") == 0.0


# ── token_length_stats ────────────────────────────────────────────────────────

class TestTokenLengthStats:
    def test_returns_expected_keys(self, tmp_path) -> None:
        path = tmp_path / "test.jsonl"
        write_jsonl(path, [
            {"prompt": "Résume le document A", "completion": "Le document A décrit une procédure."},
            {"prompt": "Résume le document B", "completion": "Le document B décrit un budget."},
        ])
        stats = token_length_stats(path)
        for section in ("prompt", "completion", "total"):
            assert section in stats
            for key in ("count", "min", "max", "mean", "p95"):
                assert key in stats[section]

    def test_empty_file(self, tmp_path) -> None:
        path = tmp_path / "empty.jsonl"
        write_jsonl(path, [])
        stats = token_length_stats(path)
        assert stats["total"]["count"] == 0

    def test_p95_within_bounds(self, tmp_path) -> None:
        import random
        random.seed(0)
        records = [
            {"prompt": "x " * random.randint(5, 50), "completion": "y " * random.randint(5, 50)}
            for _ in range(100)
        ]
        path = tmp_path / "data.jsonl"
        write_jsonl(path, records)
        stats = token_length_stats(path)
        assert stats["total"]["min"] <= stats["total"]["p95"] <= stats["total"]["max"]
