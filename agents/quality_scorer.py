"""Quality scorer agent: rate training pairs using mlx_lm.server (OpenAI-compatible API)."""

from __future__ import annotations

import argparse
import random
from pathlib import Path
from typing import Any

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.quality_scorer")

_MLX_SERVER_URL = "http://localhost:8080"
_SCORE_MODEL    = "default"  # mlx_lm.server exposes the loaded model as "default"


def _server_available() -> bool:
    try:
        import httpx  # noqa: PLC0415
        r = httpx.get(f"{_MLX_SERVER_URL}/v1/models", timeout=3.0)
        data = r.json()
        return r.status_code == 200 and len(data.get("data", [])) > 0
    except Exception:  # noqa: BLE001
        return False


def _score_pair(pair: dict[str, Any]) -> float:
    """Rate a training pair 0-1 using mlx_lm.server (OpenAI chat API). Returns 0.5 on failure."""
    domain  = pair.get("domain", "")
    prompt_text = pair.get("prompt", "")[:800]
    completion_text = pair.get("completion", "")[:800]

    user_msg = (
        f"Rate the quality of this training pair from 0 to 10.\n"
        f"Domain: {domain}\n"
        f"Consider: correctness, completeness, clarity.\n"
        f"Reply with a single integer only.\n\n"
        f"INPUT:\n{prompt_text}\n\nOUTPUT:\n{completion_text}"
    )
    try:
        import httpx  # noqa: PLC0415
        resp = httpx.post(
            f"{_MLX_SERVER_URL}/v1/chat/completions",
            json={
                "model": _SCORE_MODEL,
                "messages": [{"role": "user", "content": user_msg}],
                "max_tokens": 4,
                "temperature": 0.0,
            },
            timeout=60.0,
        )
        text = resp.json()["choices"][0]["message"]["content"].strip().split()[0]
        return float(text) / 10.0
    except Exception as exc:  # noqa: BLE001
        logger.debug("Scoring failed: %s", exc)
        return 0.5


def _heuristic_score(pair: dict[str, Any]) -> float:
    """Fallback when mlx_lm.server has no model loaded."""
    from pipeline.utils.jsonl import count_tokens  # noqa: PLC0415
    pt = count_tokens(pair.get("prompt", ""))
    ct = count_tokens(pair.get("completion", ""))
    ratio = ct / max(pt, 1)
    return min(1.0, 0.5 + (0.3 if 0.5 <= ratio <= 3.0 else 0) + (0.2 if ct > 50 else 0))


def score_dataset(
    input_path: Path | str,
    output_path: Path | str,
    batch_size: int = 50,
) -> list[dict[str, Any]]:
    """Score all pairs in a JSONL file and write results."""
    use_llm = _server_available()
    if not use_llm:
        logger.warning(
            "mlx_lm.server has no model loaded at %s — using heuristic scoring. "
            "Load a model first: mlx_lm.server --model models/<name> --port 8080",
            _MLX_SERVER_URL,
        )

    records = read_jsonl(input_path)
    scored  = []

    for i, record in enumerate(records):
        if i % batch_size == 0:
            logger.info("Scoring %d/%d", i, len(records))

        score = _score_pair(record) if use_llm else _heuristic_score(record)
        scored.append({**record, "quality_score": min(1.0, max(0.0, score))})

    write_jsonl(output_path, scored)
    mean_score = sum(r["quality_score"] for r in scored) / len(scored) if scored else 0
    logger.info("Scored %d pairs. Mean quality: %.3f", len(scored), mean_score)
    return scored


def augment(
    project: str,
    n: int,
    strategy: str = "paraphrase",
) -> list[dict[str, Any]]:
    """Generate n synthetic pairs via mlx_lm.server (requires a model loaded)."""
    if not _server_available():
        raise RuntimeError(
            "mlx_lm.server has no model loaded. "
            "Start with: mlx_lm.server --model models/<name> --port 8080"
        )

    import yaml  # noqa: PLC0415
    with open(Path("config/projects") / f"{project}.yaml") as fh:
        config = yaml.safe_load(fh)

    domain    = config.get("domain", "")
    task_type = config.get("task_type", "")

    train_path = Path(f"datalake/projects/{project}/fine-tuning-ready/train.jsonl")
    seed_pairs = read_jsonl(train_path)
    random.seed(42)
    seeds = random.sample(seed_pairs, min(n, len(seed_pairs)))

    synthetic: list[dict[str, Any]] = []
    import httpx  # noqa: PLC0415

    for seed in seeds:
        if strategy == "paraphrase":
            user_msg = (
                f"Rewrite this {domain} ({task_type}) example in a different style "
                f"while keeping identical meaning. Reply with only the rewritten text.\n\n"
                f"{seed.get('prompt', '')[:600]}"
            )
        elif strategy == "mutate":
            user_msg = (
                f"Slightly modify this {domain} example: rename 2-3 variables or terms, "
                f"keep the logic identical.\n\n{seed.get('prompt', '')[:600]}"
            )
        else:
            user_msg = seed.get("prompt", "")

        try:
            resp = httpx.post(
                f"{_MLX_SERVER_URL}/v1/chat/completions",
                json={
                    "model": _SCORE_MODEL,
                    "messages": [{"role": "user", "content": user_msg}],
                    "max_tokens": 800,
                    "temperature": 0.7,
                },
                timeout=60.0,
            )
            new_prompt = resp.json()["choices"][0]["message"]["content"].strip()
            if new_prompt:
                synthetic.append({
                    "prompt":     new_prompt,
                    "completion": seed.get("completion", ""),
                    "domain":     domain,
                    "augmented":  True,
                    "strategy":   strategy,
                })
        except Exception as exc:  # noqa: BLE001
            logger.debug("Augmentation failed: %s", exc)

    return synthetic


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    score_p = subparsers.add_parser("score")
    score_p.add_argument("--input", required=True)
    score_p.add_argument("--output", required=True)

    aug_p = subparsers.add_parser("augment")
    aug_p.add_argument("--project", required=True)
    aug_p.add_argument("--n", type=int, required=True)
    aug_p.add_argument("--strategy", default="paraphrase")

    args = parser.parse_args()

    if args.command == "score":
        score_dataset(args.input, args.output)
    elif args.command == "augment":
        pairs = augment(args.project, args.n, args.strategy)
        out = Path(f"datalake/projects/{args.project}/processed/pairs_synthetic.jsonl")
        write_jsonl(out, pairs)
        print(f"Generated {len(pairs)} synthetic pairs → {out}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
