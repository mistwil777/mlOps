"""Semantic equivalence judge — verifies that two code snippets implement the same algorithm.

Used for migration adapters (Python↔Java, etc.) to confirm that a pair is truly
a semantic match and not just a superficially similar code fragment.

Cost routing: Haiku by default, chain-of-thought reasoning.
Only called on records that have already passed syntax + similarity checks.

CLI:
    python -m agents.semantic_judge --input <file.jsonl> --src-field code_src --tgt-field code_tgt
    python -m agents.semantic_judge --code-a "..." --code-b "..." --lang-a python --lang-b java
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.semantic_judge")

_HAIKU_MODEL  = "claude-haiku-4-5-20251001"
_SONNET_MODEL = "claude-sonnet-4-6"
_DEFAULT_THRESHOLD = 0.8

_JUDGE_PROMPT = """You are a strict code equivalence judge.

Your task: determine if two code snippets implement the same algorithm / logic,
possibly in different programming languages.

They are considered EQUIVALENT if:
- They solve the same problem (same inputs → same outputs)
- They implement the same core algorithm (same complexity class, same approach)
- Variable names, comments, style differences are IRRELEVANT

They are NOT equivalent if:
- One handles edge cases the other misses
- They use fundamentally different algorithms (e.g. iterative vs recursive when both are asked)
- One is clearly wrong

Language A ({lang_a}):
```{lang_a}
{code_a}
```

Language B ({lang_b}):
```{lang_b}
{code_b}
```

Think step by step (max 3 sentences), then conclude.
Reply in this exact format:
REASONING: <your reasoning>
SCORE: <integer 0-10>
"""


def judge_pair(
    code_a: str,
    code_b: str,
    lang_a: str = "python",
    lang_b: str = "java",
    model: str = _HAIKU_MODEL,
) -> tuple[float, str]:
    """Return (score_0_to_1, reasoning). Returns (0.5, 'api_error') on failure."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — returning 0.5")
        return 0.5, "no_api_key"

    prompt = _JUDGE_PROMPT.format(
        lang_a=lang_a, code_a=code_a[:1000],
        lang_b=lang_b, code_b=code_b[:1000],
    )

    try:
        import anthropic  # noqa: PLC0415

        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        reasoning = ""
        score_raw  = 5

        for line in text.splitlines():
            if line.startswith("REASONING:"):
                reasoning = line[len("REASONING:"):].strip()
            elif line.startswith("SCORE:"):
                try:
                    score_raw = int(line[len("SCORE:"):].strip().split()[0])
                except ValueError:
                    pass

        return min(1.0, max(0.0, score_raw / 10.0)), reasoning

    except Exception as exc:  # noqa: BLE001
        logger.warning("Semantic judge failed: %s", exc)
        return 0.5, f"api_error: {exc}"


def judge_record(
    record: dict[str, Any],
    src_field: str = "code_src",
    tgt_field: str = "code_tgt",
    lang_a: str | None = None,
    lang_b: str | None = None,
) -> tuple[float, str]:
    """Judge a single record. Infers fields from schema if not specified."""
    # Infer from multilang_wiki consolidated pair format (after pair_fuser or transform)
    if src_field not in record and "prompt" in record and "completion" in record:
        # Legacy paired format — extract code blocks if present
        src_code = str(record.get("prompt", ""))
        tgt_code = str(record.get("completion", ""))
    else:
        src_code = str(record.get(src_field, ""))
        tgt_code = str(record.get(tgt_field, ""))

    if not src_code or not tgt_code:
        return 1.0, "missing_code_fields"  # pass — can't judge what isn't there

    a_lang = lang_a or str(record.get("lang_src", "python"))
    b_lang = lang_b or str(record.get("lang_tgt", "java"))

    return judge_pair(src_code, tgt_code, a_lang, b_lang)


def filter_dataset(
    input_path: Path | str,
    output_path: Path | str | None = None,
    rejected_path: Path | str | None = None,
    src_field: str = "code_src",
    tgt_field: str = "code_tgt",
    lang_a: str | None = None,
    lang_b: str | None = None,
    threshold: float = _DEFAULT_THRESHOLD,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Filter records where the two code fields are not semantically equivalent."""
    records = read_jsonl(input_path)
    passed:   list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for i, r in enumerate(records):
        if i % 20 == 0:
            logger.info("Semantic judge %d/%d", i, len(records))

        score, reasoning = judge_record(r, src_field, tgt_field, lang_a, lang_b)
        tagged = {**r, "_semantic_score": round(score, 3), "_semantic_reasoning": reasoning}

        if score >= threshold:
            passed.append(tagged)
        else:
            rejected.append({**tagged, "_semantic_reject": f"score {score:.2f} < {threshold}"})

    logger.info(
        "Semantic judge: %d passed / %d rejected (threshold=%.2f)",
        len(passed), len(rejected), threshold,
    )

    if output_path and passed:
        write_jsonl(output_path, passed)
    if rejected_path and rejected:
        write_jsonl(rejected_path, rejected)

    return passed, rejected


def main() -> None:
    parser = argparse.ArgumentParser(description="Semantic equivalence judge for code pairs")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--input",   help="JSONL file with code pairs")
    group.add_argument("--code-a",  help="First code snippet")
    parser.add_argument("--code-b",    help="Second code snippet (with --code-a)")
    parser.add_argument("--lang-a",    default="python")
    parser.add_argument("--lang-b",    default="java")
    parser.add_argument("--src-field", default="code_src")
    parser.add_argument("--tgt-field", default="code_tgt")
    parser.add_argument("--output",    help="Passed records output")
    parser.add_argument("--rejected",  help="Rejected records output")
    parser.add_argument("--threshold", type=float, default=_DEFAULT_THRESHOLD)
    args = parser.parse_args()

    if args.code_a:
        if not args.code_b:
            parser.error("--code-b required with --code-a")
        score, reasoning = judge_pair(args.code_a, args.code_b, args.lang_a, args.lang_b)
        print(f"Score: {score:.2f}")
        print(f"Reasoning: {reasoning}")
    else:
        passed, rejected = filter_dataset(
            args.input, args.output, args.rejected,
            args.src_field, args.tgt_field, args.lang_a, args.lang_b, args.threshold,
        )
        print(f"Passed: {len(passed)}  Rejected: {len(rejected)}")


if __name__ == "__main__":
    main()
