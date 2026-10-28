"""Tests for pipeline/utils/evaluators.py — domain-aware correctness evaluation."""

import sys
import pytest
from pipeline.utils.evaluators import (
    evaluate,
    EvaluatorResult,
    _eval_heuristic,
    _eval_ast_python,
    _exec_in_subprocess,
    _is_executable_python,
    _extract_code_block,
)

_skip_no_python = pytest.mark.skipif(
    sys.executable is None, reason="Python interpreter not found"
)


class TestEvalCode:
    def test_valid_python_no_reference(self):
        code = "def add(a, b):\n    return a + b\n"
        result = evaluate(code, None, "code")
        assert result.method == "ast_python"
        assert result.correct is True

    def test_syntax_error_fails(self):
        code = "def broken(:\n    pass"
        result = evaluate(code, None, "code")
        assert result.method == "ast_python"
        assert result.correct is False
        assert result.detail is not None

    def test_placeholder_only_fails(self):
        code = "def migrate(x):\n    ...\n"
        result = evaluate(code, None, "code")
        assert result.method == "ast_python"
        assert result.correct is False

    def test_pass_only_fails(self):
        code = "def migrate(x):\n    pass\n"
        result = evaluate(code, None, "code")
        assert result.method == "ast_python"
        assert result.correct is False

    @_skip_no_python
    def test_exec_with_matching_output(self):
        reference = "print(1 + 1)"
        completion = "result = 1 + 1\nprint(result)"
        result = evaluate(completion, reference, "code")
        assert result.method == "exec_python"
        assert result.correct is True

    @_skip_no_python
    def test_exec_with_wrong_output(self):
        reference = "print(42)"
        completion = "print(0)"
        result = evaluate(completion, reference, "code")
        assert result.method == "exec_python"
        assert result.correct is False

    @_skip_no_python
    def test_exec_timeout_falls_back_to_false(self):
        # Reference runs fine; completion is an infinite loop
        reference = "print('done')"
        completion = "while True: pass"
        result = evaluate(completion, reference, "code")
        assert result.method == "exec_python"
        assert result.correct is False
        assert result.detail is not None

    def test_natural_language_reference_uses_ast(self):
        # Reference is prose — should fall back to AST path
        reference = "Convert the Java method to Python and return the sum."
        code = "def sum_values(a, b):\n    return a + b\n"
        result = evaluate(code, reference, "code")
        # Not exec_python since reference is not executable
        assert result.method in ("ast_python", "rouge_l")
        assert result.correct is True

    def test_markdown_fences_stripped(self):
        code = "```python\ndef f():\n    return 1\n```"
        result = evaluate(code, None, "code")
        assert result.method == "ast_python"
        assert result.correct is True


class TestEvalText:
    def test_high_rouge_l_correct(self):
        ref = "The system migrates Java code to Python automatically."
        hyp = "The system migrates Java code to Python automatically with high accuracy."
        result = evaluate(hyp, ref, "text")
        assert result.method == "rouge_l"
        assert result.correct is True
        assert result.score > 0.4

    def test_low_rouge_l_incorrect(self):
        ref = "The system migrates Java code to Python automatically."
        hyp = "Database connection failed at runtime."
        result = evaluate(hyp, ref, "text")
        assert result.method == "rouge_l"
        assert result.correct is False

    def test_no_reference_uses_heuristic(self):
        result = evaluate("This is a valid response about migration.", None, "text")
        assert result.method == "heuristic"
        assert result.correct is True


class TestEvalDefault:
    def test_heuristic_empty_fails(self):
        result = evaluate("", None, "unknown_domain")
        assert result.correct is False

    def test_heuristic_short_fails(self):
        result = evaluate("ok", None, "unknown_domain")
        assert result.correct is False

    def test_heuristic_error_pattern_fails(self):
        for pattern in ["Error: something went wrong", "Traceback (most recent call last)"]:
            result = evaluate(pattern * 3, None, "unknown_domain")
            assert result.correct is False, f"Expected False for: {pattern!r}"

    def test_heuristic_valid_passes(self):
        result = evaluate("This is a sufficiently long valid response.", None, "unknown_domain")
        assert result.correct is True
        assert result.method == "heuristic"


class TestHelpers:
    def test_extract_code_block_with_fence(self):
        text = "```python\ndef f():\n    return 1\n```"
        assert "def f" in _extract_code_block(text)
        assert "```" not in _extract_code_block(text)

    def test_extract_code_block_plain(self):
        text = "def f():\n    return 1"
        assert _extract_code_block(text) == text

    def test_is_executable_python_true(self):
        assert _is_executable_python("print(1 + 1)") is True
        assert _is_executable_python("def f():\n    return 1") is True

    def test_is_executable_python_false_for_prose(self):
        assert _is_executable_python("Convert the Java method to Python.") is False
        assert _is_executable_python("") is False

    def test_is_executable_python_false_for_invalid(self):
        assert _is_executable_python("def broken(:") is False

    @_skip_no_python
    def test_exec_subprocess_success(self):
        stdout, err = _exec_in_subprocess("print('hello')")
        assert err is None
        assert "hello" in stdout

    @_skip_no_python
    def test_exec_subprocess_error(self):
        stdout, err = _exec_in_subprocess("raise ValueError('oops')")
        assert err is not None
        assert stdout == ""

    @_skip_no_python
    def test_exec_subprocess_timeout(self):
        stdout, err = _exec_in_subprocess("while True: pass", timeout=0.5)
        assert err is not None
        assert "timeout" in err


class TestAstEval:
    def test_function_def_passes(self):
        result = _eval_ast_python("def migrate(src):\n    return src.replace('int', 'int')\n")
        assert result is not None
        assert result.correct is True
        assert result.method == "ast_python"

    def test_empty_module_fails(self):
        result = _eval_ast_python("")
        assert result is not None
        assert result.correct is False

    def test_import_only_fails(self):
        # Only an import, no meaningful content
        result = _eval_ast_python("import os\n")
        assert result is not None
        assert result.correct is False
