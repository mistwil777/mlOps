"""Export embedded bundle — model + adapters + manifest for air-gapped deployment."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from api.db import fetchall, fetchone
from pipeline.utils.logger import get_logger

logger = get_logger("mlops.export_embedded")

_EXPORT_BASE = Path("exports/embedded")


def _load_project_config(project: str) -> dict[str, Any]:
    for candidate in (
        Path("config/models") / f"{project}.yaml",
        Path("config/projects") / f"{project}.yaml",
    ):
        if candidate.exists():
            with candidate.open() as fh:
                return yaml.safe_load(fh)
    raise FileNotFoundError(f"No config found for project '{project}'")


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _dir_sha256(directory: Path) -> str:
    """Deterministic hash of all files in a directory (sorted by relative path)."""
    h = hashlib.sha256()
    for fpath in sorted(directory.rglob("*")):
        if fpath.is_file():
            rel = str(fpath.relative_to(directory))
            h.update(rel.encode())
            h.update(_sha256(fpath).encode())
    return h.hexdigest()


def _rag_corpus_hash(project: str) -> str:
    """Hash of the RAG corpus used (LanceDB dir) to stamp the bundle."""
    rag_dir = Path(f"lancedb/{project}")
    if not rag_dir.exists():
        return "no-rag-index"
    return _dir_sha256(rag_dir)[:16]


def _get_model_info(model_id: str) -> dict[str, Any] | None:
    return fetchone("SELECT * FROM models WHERE id = ?", (model_id,))


def _get_run_info(run_id: str) -> dict[str, Any] | None:
    return fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))


def _get_evaluations(run_id: str) -> list[dict[str, Any]]:
    return fetchall("SELECT * FROM evaluations WHERE run_id = ? ORDER BY created_at DESC", (run_id,))


def export_bundle(
    model_id: str,
    *,
    output_dir: Path | None = None,
    include_adapter: bool = True,
) -> dict[str, Any]:
    """
    Create a self-contained embedded bundle from a production model.

    Bundle layout:
      <bundle_dir>/
        model/          ← fused model weights (copy)
        adapter/        ← LoRA adapter (optional)
        manifest.json   ← version, hashes, metrics, corpus stamp

    Returns: {bundle_dir, manifest_path, model_hash, corpus_hash}.
    """
    model_info = _get_model_info(model_id)
    if not model_info:
        raise ValueError(f"Model '{model_id}' not found in SQLite")

    project_id: str = model_info["project_id"]
    fused_path = Path(model_info["fused_path"]) if model_info.get("fused_path") else None

    if fused_path is None or not fused_path.exists():
        raise FileNotFoundError(f"Fused model path not found: {fused_path}")

    cfg = _load_project_config(project_id)
    run_info = _get_run_info(model_info["run_id"]) if model_info.get("run_id") else None
    evals = _get_evaluations(model_info["run_id"]) if model_info.get("run_id") else []

    # Resolve output directory
    bundle_id = f"{project_id}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"
    if output_dir is None:
        output_dir = _EXPORT_BASE / bundle_id
    output_dir.mkdir(parents=True, exist_ok=True)

    # Copy fused model
    model_dest = output_dir / "model"
    logger.info("Copying fused model %s → %s", fused_path, model_dest)
    if fused_path.is_dir():
        shutil.copytree(fused_path, model_dest, dirs_exist_ok=True)
        model_hash = _dir_sha256(model_dest)
    else:
        shutil.copy2(fused_path, model_dest)
        model_hash = _sha256(model_dest)

    # Copy adapter (optional)
    adapter_hash: str | None = None
    if include_adapter and run_info and run_info.get("adapter_path"):
        adapter_src = Path(run_info["adapter_path"])
        adapter_dest = output_dir / "adapter"
        if adapter_src.exists():
            logger.info("Copying adapter %s → %s", adapter_src, adapter_dest)
            if adapter_src.is_dir():
                shutil.copytree(adapter_src, adapter_dest, dirs_exist_ok=True)
                adapter_hash = _dir_sha256(adapter_dest)
            else:
                shutil.copy2(adapter_src, adapter_dest)
                adapter_hash = _sha256(adapter_dest)
        else:
            logger.warning("Adapter path not found: %s — skipping", adapter_src)

    # Compute RAG corpus stamp
    corpus_hash = _rag_corpus_hash(project_id)

    # Build manifest
    manifest: dict[str, Any] = {
        "bundle_id":       bundle_id,
        "bundle_version":  "1.0",
        "created_at":      datetime.now(timezone.utc).isoformat(),
        "project": {
            "id":          project_id,
            "name":        cfg.get("name", project_id),
            "domain":      cfg.get("domain", ""),
            "model":       cfg.get("model", ""),
        },
        "model": {
            "id":          model_id,
            "fused_path":  str(fused_path),
            "hash_sha256": model_hash,
            "deployed_at": model_info.get("deployed_at"),
        },
        "adapter": {
            "path":        str(run_info.get("adapter_path")) if run_info else None,
            "hash_sha256": adapter_hash,
        } if include_adapter else None,
        "training": {
            "run_id":        model_info.get("run_id"),
            "iters":         run_info.get("iters") if run_info else None,
            "lora_rank":     run_info.get("lora_rank") if run_info else None,
            "learning_rate": run_info.get("learning_rate") if run_info else None,
            "val_loss":      run_info.get("val_loss") if run_info else None,
        },
        "evaluation": [
            {
                "eval_type": e["eval_type"],
                "bleu":      e.get("bleu"),
                "rouge_l":   e.get("rouge_l"),
                "pass_rate": e.get("pass_rate"),
                "latency_ms": e.get("latency_ms"),
            }
            for e in evals
        ],
        "rag_corpus": {
            "hash":    corpus_hash,
            "comment": "Hash of LanceDB patterns used during JSONL enrichment (RAT).",
        },
        "deployment": {
            "target":    "embedded-airgap",
            "runtime":   "mlx_lm",
            "requires":  ["model/", "adapter/"],
            "excludes":  ["lancedb", "sqlite", "network"],
        },
    }

    manifest_path = output_dir / "manifest.json"
    with manifest_path.open("w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)

    logger.info("Bundle created: %s (model_hash=%s…, corpus=%s)", output_dir, model_hash[:12], corpus_hash)
    return {
        "bundle_dir":    str(output_dir),
        "manifest_path": str(manifest_path),
        "model_hash":    model_hash,
        "corpus_hash":   corpus_hash,
        "bundle_id":     bundle_id,
    }


# ── CLI ───────────────────────────────────────────────────────────────────────

def _cli() -> None:
    parser = argparse.ArgumentParser(description="Export self-contained embedded bundle")
    parser.add_argument("model_id",     help="Model ID from SQLite (models table)")
    parser.add_argument("--output",     type=Path, default=None, help="Override output directory")
    parser.add_argument("--no-adapter", action="store_true",     help="Exclude LoRA adapter from bundle")
    args = parser.parse_args()

    result = export_bundle(
        args.model_id,
        output_dir=args.output,
        include_adapter=not args.no_adapter,
    )
    print(f"Bundle dir   : {result['bundle_dir']}")
    print(f"Manifest     : {result['manifest_path']}")
    print(f"Model hash   : {result['model_hash'][:16]}…")
    print(f"Corpus hash  : {result['corpus_hash']}")


if __name__ == "__main__":
    _cli()
