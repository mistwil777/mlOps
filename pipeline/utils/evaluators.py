"""Domain-aware correctness evaluators.

Single entry point: evaluate(completion, reference, domain) -> EvaluatorResult.

Method cascade per domain:
  code  -> exec_python > ast_python > rouge_l (fallback)
  text  -> rouge_l (with reference) | heuristic (no reference)
  *     -> heuristic
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from dataclasses import dataclass

from pipeline.utils.logger import get_logger
from pipeline.utils.metrics import rouge_l_score

logger = get_logger("mlops.evaluators")

_ROUGE_L_THRESHOLD = 0.4
_EXEC_TIMEOUT = 5.0

_ERROR_PATTERNS = ("error:", "traceback", "exception:", "i'm sorry", "i cannot")
_PLACEHOLDER_PATTERNS = ("pass\n", "pass\r", "...\n", "...\r", "# TODO", "raise NotImplementedError")


@dataclass
class EvaluatorResult:
    correct: bool
    score: float        # 0–1; semantics depend on method
    method: str         # "exec_python" | "ast_python" | "rouge_l" | "heuristic"
    detail: str | None  # error message or reason for failure/fallback


def evaluate(
    completion: str,
    reference: str | None,
    domain: str,
) -> EvaluatorResult:
    """Return a correctness verdict for a completion given its domain."""
    if domain == "code":
        return _eval_code(completion, reference)
    if domain == "text":
        return _eval_text(completion, reference)
    return _eval_heuristic(completion)


# ── Domain evaluators ──────────────────────────────────────────────────────────

def _eval_code(completion: str, reference: str | None) -> EvaluatorResult:
    code = _extract_code_block(completion)

    # 1. Exec path: only when reference is itself executable Python
    if reference and _is_executable_python(reference):
        ref_stdout, ref_err = _exec_in_subprocess(reference)
        if ref_err is None:
            # Reference executes cleanly — use its stdout as ground truth
            out_stdout, out_err = _exec_in_subprocess(code)
            if out_err is None:
                match = out_stdout.strip() == ref_stdout.strip()
                return EvaluatorResult(
                    correct=match,
                    score=1.0 if match else 0.0,
                    method="exec_python",
                    detail=None if match else f"expected: {ref_stdout!r}, got: {out_stdout!r}",
                )
            return EvaluatorResult(
                correct=False,
                score=0.0,
                method="exec_python",
                detail=out_err,
            )

    # 2. AST path: static validity check
    ast_result = _eval_ast_python(code)
    if ast_result is not None:
        return ast_result

    # 3. ROUGE-L fallback
    if reference:
        score = rouge_l_score(code, reference)
        return EvaluatorResult(
            correct=score >= _ROUGE_L_THRESHOLD,
            score=score,
            method="rouge_l",
            detail="⚠ fallback — exécution et AST ont échoué",
        )

    return _eval_heuristic(completion)


def _eval_text(completion: str, reference: str | None) -> EvaluatorResult:
    if reference:
        score = rouge_l_score(completion, reference)
        return EvaluatorResult(
            correct=score >= _ROUGE_L_THRESHOLD,
            score=score,
            method="rouge_l",
            detail=None,
        )
    return _eval_heuristic(completion)


def _eval_heuristic(completion: str) -> EvaluatorResult:
    if not completion or len(completion.strip()) < 10:
        return EvaluatorResult(False, 0.0, "heuristic", "réponse trop courte ou vide")
    lower = completion.lower()
    for pattern in _ERROR_PATTERNS:
        if pattern in lower:
            return EvaluatorResult(False, 0.0, "heuristic", f"contient '{pattern}'")
    return EvaluatorResult(True, 1.0, "heuristic", None)


# ── AST validator ──────────────────────────────────────────────────────────────

def _eval_ast_python(code: str) -> EvaluatorResult | None:
    """Return an EvaluatorResult if AST parsing succeeds; None if code is unparseable."""
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return EvaluatorResult(False, 0.0, "ast_python", f"SyntaxError: {exc}")

    # Reject placeholder-only bodies (check stripped lines)
    lines = [l.strip() for l in code.splitlines()]
    for pattern in ("...", "pass", "# TODO", "raise NotImplementedError"):
        if any(l == pattern for l in lines):
            body_lines = [l for l in lines if l and not l.startswith(("def ", "class ", "#", "@"))]
            if body_lines and all(l == pattern for l in body_lines):
                return EvaluatorResult(False, 0.0, "ast_python", f"placeholder détecté : {pattern!r}")

    # Require at least one meaningful node (function/class/assignment/return)
    meaningful = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign,
                  ast.AugAssign, ast.Return, ast.Expr)
    has_content = any(isinstance(node, meaningful) for node in ast.walk(tree))
    if not has_content:
        return EvaluatorResult(False, 0.0, "ast_python", "aucune structure de code significative")

    return EvaluatorResult(True, 1.0, "ast_python", None)


# ── Subprocess execution ───────────────────────────────────────────────────────

def _exec_in_subprocess(code: str, timeout: float = _EXEC_TIMEOUT) -> tuple[str, str | None]:
    """Execute Python code in a subprocess. Returns (stdout, error_message|None)."""
    # Wrap multi-line scripts; use -c for single-expression safety
    script = textwrap.dedent(code)
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if result.returncode != 0:
            return "", result.stderr.strip() or f"exit code {result.returncode}"
        return result.stdout, None
    except subprocess.TimeoutExpired:
        return "", f"timeout ({timeout}s)"
    except Exception as exc:  # noqa: BLE001
        return "", str(exc)


def _is_executable_python(text: str) -> bool:
    """Quick check: does this text look like runnable Python (not natural language)?"""
    stripped = text.strip()
    if not stripped:
        return False
    # Must parse cleanly and contain at least one statement that isn't a bare string
    try:
        tree = ast.parse(stripped)
    except SyntaxError:
        return False
    # Reject if the entire body is only string literals (i.e. natural language description)
    non_str_nodes = [
        n for n in ast.walk(tree)
        if isinstance(n, (ast.Assign, ast.Return, ast.FunctionDef, ast.ClassDef,
                          ast.Import, ast.ImportFrom, ast.Call, ast.For, ast.While))
    ]
    return len(non_str_nodes) > 0


def _extract_code_block(text: str) -> str:
    """Strip markdown fences (```python ... ```) if present."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        # Remove first line (```python or ```) and last line (```)
        inner = lines[1:] if lines[0].startswith("```") else lines
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        return "\n".join(inner)
    return stripped
