"""Evaluation metrics: compilation checks, ROUGE-L, CodeBLEU, dataset statistics."""

from __future__ import annotations

import subprocess
import tempfile
import textwrap
from pathlib import Path
from typing import Any

from pipeline.utils.jsonl import read_jsonl, count_tokens
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.metrics")


def compile_java(code: str, timeout: int = 10) -> tuple[bool, str]:
    """Compile a Java snippet in a temp directory. Returns (success, error_message)."""
    if "class " not in code:
        code = textwrap.dedent(f"""
            public class Snippet {{
                public static void main(String[] args) {{
                    {textwrap.indent(code, '        ')}
                }}
            }}
        """)

    with tempfile.TemporaryDirectory() as tmpdir:
        src_file = Path(tmpdir) / "Snippet.java"
        src_file.write_text(code, encoding="utf-8")

        result = subprocess.run(
            ["javac", str(src_file)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=tmpdir,
        )

        if result.returncode == 0:
            return True, ""
        return False, result.stderr.strip()


def rouge_l_score(hypothesis: str, reference: str) -> float:
    """Compute ROUGE-L F1 between hypothesis and reference (character-level LCS)."""
    if not hypothesis or not reference:
        return 0.0

    h_tokens = hypothesis.split()
    r_tokens = reference.split()

    if not h_tokens or not r_tokens:
        return 0.0

    # LCS via DP
    m, n = len(h_tokens), len(r_tokens)
    dp = [[0] * (n + 1) for _ in range(2)]
    lcs_len = 0
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            if h_tokens[i - 1] == r_tokens[j - 1]:
                dp[i % 2][j] = dp[(i - 1) % 2][j - 1] + 1
                lcs_len = max(lcs_len, dp[i % 2][j])
            else:
                dp[i % 2][j] = 0

    # ROUGE-L F1
    precision = lcs_len / m if m else 0.0
    recall    = lcs_len / n if n else 0.0
    if precision + recall == 0:
        return 0.0
    return round(2 * precision * recall / (precision + recall), 4)


def codebLEU_score(hypothesis: str, reference: str, lang: str = "java") -> float:
    """Compute CodeBLEU score between hypothesis and reference code.

    Falls back to ROUGE-L if codebleu/sacrebleu are not installed.
    """
    try:
        from codebleu import calc_codebleu  # noqa: PLC0415

        result = calc_codebleu(
            [reference],
            [hypothesis],
            lang=lang,
            weights=(0.25, 0.25, 0.25, 0.25),
        )
        return float(result["codebleu"])
    except ImportError:
        logger.warning("codebleu not installed, falling back to sacrebleu BLEU")
        try:
            from sacrebleu.metrics import BLEU  # noqa: PLC0415

            bleu = BLEU(effective_order=True)
            score = bleu.sentence_score(hypothesis, [reference])
            return score.score / 100.0
        except Exception as exc:  # noqa: BLE001
            logger.warning("sacrebleu failed (%s), using ROUGE-L", exc)
            return rouge_l_score(hypothesis, reference)


def token_length_stats(jsonl_path: Path | str) -> dict[str, float]:
    """Return token length statistics for a JSONL file."""
    import statistics  # noqa: PLC0415

    records = read_jsonl(jsonl_path)
    prompt_lengths = [count_tokens(r.get("prompt", "")) for r in records]
    completion_lengths = [count_tokens(r.get("completion", "")) for r in records]
    total_lengths = [p + c for p, c in zip(prompt_lengths, completion_lengths)]

    def _stats(lengths: list[int]) -> dict[str, float]:
        if not lengths:
            return {"count": 0, "min": 0, "max": 0, "mean": 0.0, "p95": 0}
        s = sorted(lengths)
        p95_idx = max(0, int(len(s) * 0.95) - 1)
        return {
            "count": len(lengths),
            "min": min(lengths),
            "max": max(lengths),
            "mean": round(statistics.mean(lengths), 1),
            "p95": s[p95_idx],
        }

    return {
        "prompt": _stats(prompt_lengths),
        "completion": _stats(completion_lengths),
        "total": _stats(total_lengths),
    }


def quality_score_summary(jsonl_path: Path | str) -> dict[str, Any]:
    """Return distribution of quality_score field in a JSONL file."""
    records = read_jsonl(jsonl_path)
    scores = [r["quality_score"] for r in records if "quality_score" in r]

    if not scores:
        return {"count": 0, "scored": 0, "distribution": {}}

    import statistics  # noqa: PLC0415

    buckets = {"0.0-0.5": 0, "0.5-0.7": 0, "0.7-0.9": 0, "0.9-1.0": 0}
    for s in scores:
        if s < 0.5:
            buckets["0.0-0.5"] += 1
        elif s < 0.7:
            buckets["0.5-0.7"] += 1
        elif s < 0.9:
            buckets["0.7-0.9"] += 1
        else:
            buckets["0.9-1.0"] += 1

    return {
        "count": len(records),
        "scored": len(scores),
        "mean": round(statistics.mean(scores), 3),
        "median": round(statistics.median(scores), 3),
        "distribution": buckets,
    }
