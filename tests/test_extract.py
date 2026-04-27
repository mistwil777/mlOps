"""Tests for pipeline/extract.py."""

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
    return PairExtractor(
        domain="code",
        task_type="migration",
        src_lang="python",
        tgt_lang="java",
        min_tokens=10,
        max_tokens=512,
    )


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
            assert "rag_context" in p
            assert "meta" in p
            assert p["meta"]["tokens"] > 0
            assert p["meta"]["domain"] == "code"
            assert p["meta"]["task_type"] == "migration"

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
            assert "rag_context" in f
            assert "meta" in f

    def test_format_qwen(self, extractor: PairExtractor, repo_with_pairs: Path) -> None:
        pairs = extractor.extract_from_repo(repo_with_pairs)
        formatted = extractor.format_as_jsonl(pairs, model_format="qwen")
        for f in formatted:
            assert "<|im_start|>user" in f["prompt"]
            assert "<|im_end|>" in f["prompt"]

    def test_instruction_contains_task(self, extractor: PairExtractor, repo_with_pairs: Path) -> None:
        pairs = extractor.extract_from_repo(repo_with_pairs)
        formatted = extractor.format_as_jsonl(pairs, model_format="raw")
        for f in formatted:
            assert "python" in f["prompt"].lower() or "java" in f["prompt"].lower()

    def test_min_tokens_filter(self, repo_with_pairs: Path) -> None:
        ex = PairExtractor(
            domain="code", task_type="migration",
            src_lang="python", tgt_lang="java",
            min_tokens=1000, max_tokens=9999,
        )
        pairs = ex.extract_from_repo(repo_with_pairs)
        assert all(p["meta"]["tokens"] >= 1000 for p in pairs), "All pairs should meet min_tokens"

    def test_missing_repo_raises(self, extractor: PairExtractor) -> None:
        with pytest.raises(FileNotFoundError):
            extractor.extract_from_repo("/nonexistent/path/to/repo")

    def test_standalone_extraction(self, tmp_path: Path) -> None:
        (tmp_path / "doc.txt").write_text("This is a long enough document with many words for standalone extraction test.\n" * 3)
        ex = PairExtractor(domain="legal", task_type="summarization", src_lang=None, tgt_lang=None, min_tokens=10)
        pairs = ex.extract_from_repo(tmp_path)
        assert len(pairs) >= 1
        for p in pairs:
            assert p["meta"]["domain"] == "legal"
            assert p["meta"]["task_type"] == "summarization"
