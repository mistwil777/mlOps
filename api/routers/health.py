"""System health check."""

from __future__ import annotations

import shutil

import httpx
from fastapi import APIRouter

router = APIRouter(prefix="/api/health", tags=["health"])


async def _check(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get(url)
            return r.status_code < 500
    except Exception:
        return False


@router.get("")
async def health():
    disk = shutil.disk_usage(".")
    disk_free_gb = round(disk.free / 1e9, 1)

    mlx_server = await _check("http://localhost:8080/v1/models")
    mlflow     = await _check("http://localhost:5001/health")
    # serving = mlx_server has a loaded model (data list non-empty)
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            r = await client.get("http://localhost:8080/v1/models")
            serving = r.status_code == 200 and len(r.json().get("data", [])) > 0
    except Exception:
        serving = False

    return {
        "mlx_server":   mlx_server,
        "mlflow":       mlflow,
        "serving":      serving,
        "disk_free_gb": disk_free_gb,
    }
