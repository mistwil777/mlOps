"""Tests for pipeline/evaluate.py and pipeline/utils/metrics.py."""

import shutil
import subprocess
import pytest

from pipeline.utils.metrics import compile_java, codebLEU_score, token_length_stats
from pipeline.utils.jsonl import write_jsonl


def _javac_functional() -> bool:
    """Return True only if javac is installed AND actually works (not a macOS shim)."""
    if not shutil.which("javac"):
        return False
    try:
        result = subprocess.run(["javac", "--version"], capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:  # noqa: BLE001
        return False


_JAVAC_AVAILABLE = _javac_functional()
_skip_no_javac = pytest.mark.skipif(not _JAVAC_AVAILABLE, reason="javac not available or not functional")


# ── compile_java ──────────────────────────────────────────────────────────────

class TestCompileJava:
    @_skip_no_javac
    def test_valid_java_compiles(self) -> None:
        code = "public class Hello { public static void main(String[] a) { System.out.println(\"hi\"); } }"
        ok, err = compile_java(code)
        assert ok is True
        assert err == ""

    @_skip_no_javac
    def test_invalid_java_fails(self) -> None:
        code = "public class Broken { public static void oops( { } }"
        ok, err = compile_java(code)
        assert ok is False
        assert len(err) > 0

    @_skip_no_javac
    def test_bare_snippet_wraps_in_class(self) -> None:
        code = "int x = 42;"
        ok, _ = compile_java(code)
        assert ok is True

    def test_compile_returns_bool_and_string(self) -> None:
        ok, err = compile_java("")
        assert isinstance(ok, bool)
        assert isinstance(err, str)

    @_skip_no_javac
    def test_syntax_error_returns_message(self) -> None:
        _, err = compile_java("class Foo { void bar() { return 1 }")
        assert isinstance(err, str) and len(err) > 0


# ── codebLEU_score ────────────────────────────────────────────────────────────

class TestCodeBLEU:
    def test_identical_strings_max_score(self) -> None:
        code = "public int add(int a, int b) { return a + b; }"
        score = codebLEU_score(code, code)
        assert score > 0.8, f"Identical strings should score high, got {score}"

    def test_unrelated_strings_low_score(self) -> None:
        hyp = "public int foo() { return 1; }"
        ref = "SELECT name FROM users WHERE id = 42;"
        score = codebLEU_score(hyp, ref)
        assert score < 0.5, f"Unrelated strings should score low, got {score}"

    def test_score_in_range(self) -> None:
        hyp = "public int add(int a, int b) { return a + b; }"
        ref = "public int add(int x, int y) { return x + y; }"
        score = codebLEU_score(hyp, ref)
        assert 0.0 <= score <= 1.0


# ── token_length_stats ────────────────────────────────────────────────────────

class TestTokenLengthStats:
    def test_returns_expected_keys(self, tmp_path) -> None:
        path = tmp_path / "test.jsonl"
        write_jsonl(path, [
            {"prompt": "def add(a, b): return a + b", "completion": "public int add(int a, int b) { return a + b; }"},
            {"prompt": "def sub(a, b): return a - b", "completion": "public int sub(int a, int b) { return a - b; }"},
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
