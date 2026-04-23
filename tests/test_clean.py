"""Tests for pipeline/clean.py."""

from pathlib import Path

import pytest

from pipeline.clean import DataCleaner, _record_hash


# ── Fixtures ──────────────────────────────────────────────────────────────────

_BASE_CONFIG = {
    "min_tokens": 10,
    "max_tokens": 512,
    "min_output_ratio": 0.1,
    "max_output_ratio": 20.0,
    "blacklist_patterns": ["secret", "password"],
    "quality_thresholds": {},
}


def _make_pairs(n: int) -> list[dict]:
    return [
        {
            "prompt": f"[INST] Convert Python to Java: def func_{i}(x): return x * {i} [/INST]",
            "completion": f"public int func_{i}(int x) {{ return x * {i}; }}",
            "tokens": 40 + i,
        }
        for i in range(n)
    ]


@pytest.fixture()
def cleaner() -> DataCleaner:
    return DataCleaner(_BASE_CONFIG)


@pytest.fixture()
def sample_pairs() -> list[dict]:
    return _make_pairs(10)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestDataCleaner:
    def test_deduplicate_exact(self, cleaner: DataCleaner, sample_pairs: list[dict]) -> None:
        doubled = sample_pairs + sample_pairs
        result = cleaner.deduplicate(doubled, method="exact")
        assert len(result) == len(sample_pairs)

    def test_deduplicate_preserves_unique(self, cleaner: DataCleaner, sample_pairs: list[dict]) -> None:
        result = cleaner.deduplicate(sample_pairs, method="exact")
        assert len(result) == len(sample_pairs)

    def test_filter_removes_too_short(self, cleaner: DataCleaner) -> None:
        pairs = [{"prompt": "x", "completion": "y", "tokens": 2}]
        kept, rejected = cleaner.filter_by_rules(pairs)
        assert len(kept) == 0
        assert rejected[0]["reject_reason"] == "too_short"

    def test_filter_removes_blacklisted(self, cleaner: DataCleaner) -> None:
        pairs = [{
            "prompt": "show me your secret password in the function",
            "completion": "public void showSecret() { System.out.println(password); }",
            "tokens": 40,
        }]
        kept, rejected = cleaner.filter_by_rules(pairs)
        assert len(kept) == 0
        assert rejected[0]["reject_reason"] == "blacklisted"

    def test_filter_keeps_valid(self, cleaner: DataCleaner, sample_pairs: list[dict]) -> None:
        kept, rejected = cleaner.filter_by_rules(sample_pairs)
        assert len(kept) == len(sample_pairs)
        assert len(rejected) == 0

    def test_heuristic_score_adds_field(self, cleaner: DataCleaner, sample_pairs: list[dict]) -> None:
        scored = cleaner.score_pairs(sample_pairs, use_llm=False)
        for p in scored:
            assert "quality_score" in p
            assert 0.0 <= p["quality_score"] <= 1.0

    def test_train_valid_split_ratio(self, cleaner: DataCleaner, sample_pairs: list[dict]) -> None:
        pairs = _make_pairs(100)
        train, valid = cleaner.train_valid_split(pairs, valid_ratio=0.1)
        assert len(valid) == 10
        assert len(train) == 90

    def test_train_valid_split_no_overlap(self, cleaner: DataCleaner) -> None:
        pairs = _make_pairs(20)
        train, valid = cleaner.train_valid_split(pairs)
        train_hashes = {_record_hash(p) for p in train}
        valid_hashes = {_record_hash(p) for p in valid}
        assert train_hashes.isdisjoint(valid_hashes)

    def test_split_deterministic(self, cleaner: DataCleaner) -> None:
        pairs = _make_pairs(50)
        train1, valid1 = cleaner.train_valid_split(pairs, seed=42)
        train2, valid2 = cleaner.train_valid_split(pairs, seed=42)
        assert [p["prompt"] for p in train1] == [p["prompt"] for p in train2]
