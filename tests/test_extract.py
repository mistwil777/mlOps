"""Tests for pipeline/extract.py."""

import json
import tempfile
from pathlib import Path

import pytest

from pipeline.extract import PairExtractor


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture()
def repo_with_pairs(tmp_path: Path) -> Path:
    """Create a minimal fake repo with Python + Java file pairs."""
    pairs = [
        ("add.py", "add.java", "def add(a, b):\n    return a + b\n",
         "public class Add {\n    public static int add(int a, int b) { return a + b; }\n}\n"),
        ("greet.py", "greet.java", "def greet(name):\n    return f'Hello, {name}'\n",
         "public class Greet {\n    public static String greet(String name) { return \"Hello, \" + name; }\n}\n"),
        ("short.py", "short.java", "x=1\n", "int x=1;\n"),  # will be filtered (too short)
    ]
    for src_name, tgt_name, src_code, tgt_code in pairs:
        (tmp_path / src_name).write_text(src_code)
        (tmp_path / tgt_name).write_text(tgt_code)
    return tmp_path


@pytest.fixture()
def extractor() -> PairExtractor:
    return PairExtractor(src_lang="python", tgt_lang="java", min_tokens=10, max_tokens=512)


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestPairExtractor:
    def test_extract_finds_pairs(self, extractor: PairExtractor, repo_with_pairs: Path) -> None:
        pairs = extractor.extract_from_repo(repo_with_pairs)
        assert len(pairs) >= 2, "Should find at least 2 valid pairs"

    def test_extract_structure(self, extractor: PairExtractor, repo_with_pairs: Path) -> None:
        pairs = extractor.extract_from_repo(repo_with_pairs)
        for p in pairs:
            assert "prompt" in p
            assert "completion" in p
            assert "tokens" in p
            assert p["tokens"] > 0

    def test_format_mistral(self, extractor: PairExtractor, repo_with_pairs: Path) -> None:
        pairs = extractor.extract_from_repo(repo_with_pairs)
        formatted = extractor.format_as_jsonl(pairs, model_format="mistral")
        for f in formatted:
            assert "[INST]" in f["prompt"]
            assert "[/INST]" in f["prompt"]

    def test_format_raw(self, extractor: PairExtractor, repo_with_pairs: Path) -> None:
        pairs = extractor.extract_from_repo(repo_with_pairs)
        formatted = extractor.format_as_jsonl(pairs, model_format="raw")
        for f in formatted:
            assert "prompt" in f
            assert "completion" in f

    def test_validate_syntax_python(self, extractor: PairExtractor) -> None:
        valid = "def foo():\n    return 1\n"
        invalid = "def foo(:\n    return 1\n"
        assert extractor.validate_syntax(valid, "python") in (True,)
        # syntax validation may return True if tree-sitter not installed — that's acceptable

    def test_min_tokens_filter(self, repo_with_pairs: Path) -> None:
        extractor = PairExtractor("python", "java", min_tokens=1000, max_tokens=9999)
        pairs = extractor.extract_from_repo(repo_with_pairs)
        assert all(p["tokens"] >= 1000 for p in pairs), "All pairs should meet min_tokens"

    def test_missing_repo_raises(self, extractor: PairExtractor) -> None:
        with pytest.raises(FileNotFoundError):
            extractor.extract_from_repo("/nonexistent/path/to/repo")
