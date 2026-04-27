"""Base model catalog — reads config/models/*.yaml + manages HuggingFace downloads."""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from api.config import get_base_model, list_base_models, TASK_TYPES, DOMAINS

router = APIRouter(prefix="/api/base-models", tags=["base-models"])

# In-memory download progress: model_id → {status, progress, error}
_downloads: dict[str, dict[str, Any]] = {}


def _model_local_path(hf_path: str) -> Path:
    """Canonical local path: models/<org>/<repo-name>."""
    parts = hf_path.split("/")
    return Path("models") / parts[0] / parts[1] if len(parts) == 2 else Path("models") / hf_path


def _is_downloaded(hf_path: str) -> bool:
    p = _model_local_path(hf_path)
    return p.exists() and any(p.iterdir())


def _validate_hf_repo(hf_path: str) -> tuple[bool, str]:
    """
    Check that the HuggingFace repo exists and contains MLX-compatible weights.
    Returns (ok, error_message).
    """
    try:
        import httpx  # noqa: PLC0415
        api_url = f"https://huggingface.co/api/models/{hf_path}"
        r = httpx.get(api_url, timeout=10.0)
        if r.status_code == 404:
            return False, f"Repository '{hf_path}' not found on HuggingFace"
        if r.status_code != 200:
            return False, f"HuggingFace API returned {r.status_code}"

        data = r.json()
        # Check licence is open
        licence = (data.get("cardData") or {}).get("license", "")
        blocked = ["gpl", "agpl", "cc-by-nc", "llama"]
        if any(b in licence.lower() for b in blocked):
            return False, f"Licence '{licence}' may restrict commercial use — verify before fine-tuning"

        # Check repo contains at least one .safetensors or config.json (MLX indicator)
        siblings = [f["rfilename"] for f in data.get("siblings", [])]
        mlx_files = [f for f in siblings if f.endswith((".safetensors", "config.json"))]
        if not mlx_files:
            return False, f"Repo '{hf_path}' contains no .safetensors or config.json — may not be a valid model"

        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"Validation failed: {exc}"


def _download_worker(model_id: str, hf_path: str) -> None:
    """Background thread: huggingface-cli download → models/<hf_path>."""
    dest = _model_local_path(hf_path)
    dest.mkdir(parents=True, exist_ok=True)
    _downloads[model_id] = {"status": "downloading", "progress": 0, "error": None}

    try:
        cmd = [
            sys.executable, "-m", "huggingface_hub.commands.huggingface_cli",
            "download", hf_path,
            "--local-dir", str(dest),
            "--local-dir-use-symlinks", "False",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        lines: list[str] = []
        for line in proc.stdout:  # type: ignore[union-attr]
            lines.append(line.strip())
            # Parse rough progress from huggingface_hub output
            if "%" in line:
                try:
                    pct = int(line.split("%")[0].split()[-1])
                    _downloads[model_id]["progress"] = pct
                except (ValueError, IndexError):
                    pass

        proc.wait()
        if proc.returncode == 0:
            _downloads[model_id] = {"status": "completed", "progress": 100, "error": None}
        else:
            _downloads[model_id] = {
                "status": "failed",
                "progress": 0,
                "error": "\n".join(lines[-5:]),
            }
    except Exception as exc:  # noqa: BLE001
        _downloads[model_id] = {"status": "failed", "progress": 0, "error": str(exc)}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("")
def list_models():
    models = list_base_models()
    for m in models:
        m["downloaded"]  = _is_downloaded(m["hf_path"])
        m["local_path"]  = str(_model_local_path(m["hf_path"])) if m["downloaded"] else None
        m["download_status"] = _downloads.get(m["id"], {}).get("status", "idle")
    return models


@router.get("/task-types")
def list_task_types():
    return TASK_TYPES


@router.get("/domains")
def list_domains():
    return DOMAINS


@router.get("/{model_id}")
def get_model(model_id: str):
    m = get_base_model(model_id)
    if not m:
        raise HTTPException(404, f"Model '{model_id}' not found")
    m["downloaded"]  = _is_downloaded(m["hf_path"])
    m["local_path"]  = str(_model_local_path(m["hf_path"])) if m["downloaded"] else None
    m["download_status"] = _downloads.get(model_id, {}).get("status", "idle")
    return m


class DownloadRequest(BaseModel):
    validate_first: bool = True


@router.post("/{model_id}/download")
def start_download(model_id: str, body: DownloadRequest = DownloadRequest()):
    m = get_base_model(model_id)
    if not m:
        raise HTTPException(404, f"Model '{model_id}' not found")

    if _is_downloaded(m["hf_path"]):
        return {"status": "already_downloaded", "local_path": str(_model_local_path(m["hf_path"]))}

    current = _downloads.get(model_id, {})
    if current.get("status") == "downloading":
        return {"status": "downloading", "progress": current.get("progress", 0)}

    if body.validate_first:
        ok, err = _validate_hf_repo(m["hf_path"])
        if not ok:
            raise HTTPException(422, f"Validation failed: {err}")

    t = threading.Thread(target=_download_worker, args=(model_id, m["hf_path"]), daemon=True)
    t.start()
    return {"status": "started", "model_id": model_id, "hf_path": m["hf_path"]}


@router.get("/{model_id}/download/status")
def download_status(model_id: str):
    m = get_base_model(model_id)
    if not m:
        raise HTTPException(404, f"Model '{model_id}' not found")

    if _is_downloaded(m["hf_path"]):
        return {"status": "completed", "progress": 100, "local_path": str(_model_local_path(m["hf_path"]))}

    return _downloads.get(model_id, {"status": "idle", "progress": 0, "error": None})


@router.post("/{model_id}/validate")
def validate_model(model_id: str):
    m = get_base_model(model_id)
    if not m:
        raise HTTPException(404, f"Model '{model_id}' not found")
    ok, err = _validate_hf_repo(m["hf_path"])
    return {"valid": ok, "hf_path": m["hf_path"], "error": err or None}
