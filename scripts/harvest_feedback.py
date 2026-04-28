"""Harvest human corrections from production feedback → inject into fine-tuning dataset."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml


_API_BASE = "http://localhost:8000"


def _fetch_corrections(project: str, model_id: str | None, limit: int) -> list[dict]:
    url = f"{_API_BASE}/api/feedback/corrections/{project}"
    params: dict = {"limit": limit}
    if model_id:
        params["model_id"] = model_id
    resp = httpx.get(url, params=params, timeout=30.0)
    resp.raise_for_status()
    return resp.json()


def _to_training_pair(record: dict) -> dict:
    """Convert a feedback correction record to a fine-tuning JSONL pair."""
    return {
        "prompt": record["prompt"],
        "completion": record["correction"],
        "source": "feedback",
        "feedback_id": record["id"],
        "model_id": record["model_id"],
    }


def _load_blacklist(project: str) -> list[str]:
    path = Path("config/projects") / f"{project}.yaml"
    if not path.exists():
        return []
    cfg = yaml.safe_load(path.read_text())
    return cfg.get("blacklist_patterns", [])


def _passes_blacklist(pair: dict, patterns: list[str]) -> bool:
    text = json.dumps(pair)
    return not any(p.lower() in text.lower() for p in patterns)


def harvest(
    project: str,
    model_id: str | None = None,
    limit: int = 500,
    dry_run: bool = False,
) -> int:
    corrections = _fetch_corrections(project, model_id, limit)
    if not corrections:
        print(f"No corrections found for project={project}")
        return 0

    blacklist = _load_blacklist(project)
    pairs = [_to_training_pair(r) for r in corrections]
    pairs = [p for p in pairs if _passes_blacklist(p, blacklist)]

    if not pairs:
        print("All corrections filtered by blacklist — nothing to write")
        return 0

    out_dir = Path("datalake/projects") / project / "raw" / "feedback"
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_file = out_dir / f"feedback_{ts}.jsonl"

    if dry_run:
        print(f"DRY RUN — would write {len(pairs)} pairs to {out_file}")
        for p in pairs[:5]:
            print(f"  prompt: {p['prompt'][:80]!r}")
            print(f"  correction: {p['completion'][:80]!r}")
        return len(pairs)

    out_file.write_text("\n".join(json.dumps(p, ensure_ascii=False) for p in pairs))
    print(f"Wrote {len(pairs)} feedback pairs → {out_file}")
    print("Next: run /clean-dataset to process, then /train-lora to fine-tune on corrections")
    return len(pairs)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Harvest feedback corrections into datalake")
    parser.add_argument("--project", required=True)
    parser.add_argument("--model-id", help="Filter to a specific model version")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    n = harvest(
        project=args.project,
        model_id=args.model_id,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    sys.exit(0 if n >= 0 else 1)


if __name__ == "__main__":
    main()
