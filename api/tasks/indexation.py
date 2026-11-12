"""Celery tasks — norm indexation (RAG + KG) and raw file storage."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from celery import Task

from api.celery_app import celery_app


class _ProgressTask(Task):
    """Base task that exposes update_progress helper."""

    def update_progress(self, current: int, total: int, message: str, extra: dict | None = None) -> None:
        self.update_state(
            state="PROGRESS",
            meta={"current": current, "total": total, "message": message, **(extra or {})},
        )


@celery_app.task(
    bind=True,
    base=_ProgressTask,
    name="mlops.index_norms",
    max_retries=2,
    soft_time_limit=600,
    time_limit=660,
)
def index_norms(
    self: _ProgressTask,
    project_id: str,
    file_paths: list[str],
    domain: str = "general",
    source_description: str = "norm",
) -> dict[str, Any]:
    """Index norm/template files into RAG + KG.

    file_paths: absolute paths to already-saved temp files.
    Returns aggregated results dict.
    """
    from pair_forge.extractors import extract_content  # noqa: PLC0415
    from pipeline.rag_index import IngestionRagIndex   # noqa: PLC0415

    rag = IngestionRagIndex(project_id)
    total = len(file_paths)

    try:
        from pipeline.utils.knowledge_graph import ProjectKG  # noqa: PLC0415
        import boto3  # noqa: PLC0415
        from botocore.config import Config as BotoConfig  # noqa: PLC0415

        def _llm_fn(prompt: str, cfg: dict = {}, max_tokens: int = 1024) -> str | None:  # noqa: B006
            client = boto3.client(
                "bedrock-runtime", region_name="us-east-1",
                config=BotoConfig(connect_timeout=10, read_timeout=120),
            )
            resp = client.converse(
                modelId="us.amazon.nova-pro-v1:0",
                messages=[{"role": "user", "content": [{"text": prompt}]}],
                inferenceConfig={"maxTokens": max_tokens, "temperature": 0.1},
            )
            return resp["output"]["message"]["content"][0]["text"].strip()

        kg = ProjectKG(project_id)
    except Exception:  # noqa: BLE001
        kg = None
        _llm_fn = None  # type: ignore[assignment]

    results: list[dict[str, Any]] = []

    for i, raw_path in enumerate(file_paths):
        p = Path(raw_path)
        filename = p.name
        self.update_progress(i, total, f"Extraction : {filename}")

        try:
            text, method = extract_content(p)
        except Exception as exc:  # noqa: BLE001
            results.append({"file": filename, "error": f"Extraction failed: {exc}"})
            p.unlink(missing_ok=True)
            continue

        if not text.strip():
            results.append({"file": filename, "error": "No text extracted"})
            p.unlink(missing_ok=True)
            continue

        self.update_progress(i, total, f"RAG embedding : {filename}")
        rag.ingest_norm(text, source=source_description, domain=domain)

        kg_ok = False
        if kg is not None and _llm_fn is not None:
            self.update_progress(i, total, f"KG extraction : {filename}")
            try:
                kg.ingest_norm_document(text, domain=domain, llm_fn=_llm_fn)
                kg_ok = True
            except Exception:  # noqa: BLE001
                pass

        p.unlink(missing_ok=True)
        results.append({
            "file": filename,
            "method": method,
            "text_length": len(text),
            "kg_ingested": kg_ok,
        })

    return {
        "project_id": project_id,
        "files_processed": len(results),
        "rag_chunks_total": rag.count,
        "results": results,
    }


@celery_app.task(
    bind=True,
    base=_ProgressTask,
    name="mlops.store_raw_files",
    soft_time_limit=120,
    time_limit=180,
)
def store_raw_files(
    self: _ProgressTask,
    project_id: str,
    file_paths: list[str],
) -> dict[str, Any]:
    """Move already-saved temp files into datalake/raw/."""
    import shutil  # noqa: PLC0415

    raw_dir = Path(f"datalake/projects/{project_id}/raw")
    raw_dir.mkdir(parents=True, exist_ok=True)
    total = len(file_paths)
    saved: list[str] = []
    errors: list[dict[str, str]] = []

    for i, raw_path in enumerate(file_paths):
        p = Path(raw_path)
        self.update_progress(i, total, f"Stockage : {p.name}")
        try:
            dest = raw_dir / p.name
            shutil.move(str(p), dest)
            saved.append(p.name)
        except Exception as exc:  # noqa: BLE001
            errors.append({"file": p.name, "error": str(exc)})

    return {
        "project_id": project_id,
        "saved": saved,
        "errors": errors,
        "raw_dir": str(raw_dir),
        "total_files": len(list(raw_dir.iterdir())),
    }
