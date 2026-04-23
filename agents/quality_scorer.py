"""Quality scorer agent: rate training pairs using a local LLM (Ollama)."""

from __future__ import annotations

import argparse
import random
from pathlib import Path
from typing import Any

from pipeline.utils.jsonl import read_jsonl, write_jsonl
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.quality_scorer")

_OLLAMA_URL = "http://localhost:11434"
_SCORE_MODEL = "mistral"


def _ollama_available() -> bool:
    try:
        import httpx  # noqa: PLC0415
        r = httpx.get(f"{_OLLAMA_URL}/api/tags", timeout=3.0)
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _score_pair(pair: dict[str, Any], model: str = _SCORE_MODEL) -> float:
    """Rate a pair 0-1 using Ollama. Returns 0.5 on failure."""
    prompt = (
        "Rate the quality of this code translation pair from 0 to 10. "
        "Consider: correctness, completeness, idiomatic style. "
        "Reply with a single integer only.\n\n"
        f"SOURCE ({pair.get('src_lang','source')}):\n{pair.get('prompt','')[:800]}\n\n"
        f"TARGET ({pair.get('tgt_lang','target')}):\n{pair.get('completion','')[:800]}"
    )
    try:
        import httpx  # noqa: PLC0415
        resp = httpx.post(
            f"{_OLLAMA_URL}/api/generate",
            json={"model": model, "prompt": prompt, "stream": False},
            timeout=60.0,
        )
        text = resp.json().get("response", "5").strip().split()[0]
        return float(text) / 10.0
    except Exception as exc:  # noqa: BLE001
        logger.debug("Scoring failed: %s", exc)
        return 0.5


def score_dataset(
    input_path: Path | str,
    output_path: Path | str,
    batch_size: int = 50,
    model: str = _SCORE_MODEL,
) -> list[dict[str, Any]]:
    """Score all pairs in a JSONL file and write results."""
    if not _ollama_available():
        logger.warning("Ollama not available at %s — using heuristic scoring", _OLLAMA_URL)

    records = read_jsonl(input_path)
    scored = []

    for i, record in enumerate(records):
        if i % batch_size == 0:
            logger.info("Scoring %d/%d", i, len(records))

        if _ollama_available():
            score = _score_pair(record, model)
        else:
            from pipeline.utils.jsonl import count_tokens  # noqa: PLC0415
            pt = count_tokens(record.get("prompt", ""))
            ct = count_tokens(record.get("completion", ""))
            ratio = ct / max(pt, 1)
            score = 0.5 + (0.3 if 0.5 <= ratio <= 3.0 else 0) + (0.2 if ct > 50 else 0)

        scored.append({**record, "quality_score": min(1.0, max(0.0, score))})

    write_jsonl(output_path, scored)
    mean_score = sum(r["quality_score"] for r in scored) / len(scored) if scored else 0
    logger.info("Scored %d pairs. Mean quality: %.3f", len(scored), mean_score)
    return scored


def augment(
    project: str,
    n: int,
    strategy: str = "paraphrase",
    model: str = _SCORE_MODEL,
) -> list[dict[str, Any]]:
    """Generate n synthetic pairs via LLM augmentation."""
    if not _ollama_available():
        raise RuntimeError("Ollama must be running for augmentation. Start with: ollama serve")

    import yaml  # noqa: PLC0415
    with open(Path("config/projects") / f"{project}.yaml") as fh:
        config = yaml.safe_load(fh)

    src_lang = config["source_lang"]
    tgt_lang = config["target_lang"]

    train_path = Path(f"datalake/projects/{project}/fine-tuning-ready/train.jsonl")
    seed_pairs = read_jsonl(train_path)
    random.seed(42)
    seeds = random.sample(seed_pairs, min(n, len(seed_pairs)))

    synthetic = []
    import httpx  # noqa: PLC0415

    for seed in seeds:
        if strategy == "paraphrase":
            prompt = (
                f"Rewrite this {src_lang} code in a different style (rename variables, "
                f"change loop style) while keeping identical behavior. "
                f"Reply with only the rewritten code.\n\n{seed.get('prompt','')[:600]}"
            )
        elif strategy == "mutate":
            prompt = (
                f"Slightly modify this {src_lang} code: rename 2-3 variables, "
                f"add a type hint or comment. Keep the logic identical.\n\n"
                f"{seed.get('prompt','')[:600]}"
            )
        else:
            prompt = seed.get("prompt", "")

        try:
            resp = httpx.post(
                f"{_OLLAMA_URL}/api/generate",
                json={"model": model, "prompt": prompt, "stream": False},
                timeout=60.0,
            )
            new_prompt = resp.json().get("response", "").strip()
            if new_prompt:
                synthetic.append({
                    "prompt": new_prompt,
                    "completion": seed.get("completion", ""),
                    "augmented": True,
                    "strategy": strategy,
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
    score_p.add_argument("--model", default=_SCORE_MODEL)

    aug_p = subparsers.add_parser("augment")
    aug_p.add_argument("--project", required=True)
    aug_p.add_argument("--n", type=int, required=True)
    aug_p.add_argument("--strategy", default="paraphrase")

    args = parser.parse_args()

    if args.command == "score":
        score_dataset(args.input, args.output, model=args.model)
    elif args.command == "augment":
        pairs = augment(args.project, args.n, args.strategy)
        out = Path(f"datalake/projects/{args.project}/processed/pairs_synthetic.jsonl")
        write_jsonl(out, pairs)
        print(f"Generated {len(pairs)} synthetic pairs → {out}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
