"""Read project and model YAML configs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

MODELS_DIR   = Path("config/models")
PROJECTS_DIR = Path("config/projects")

TASK_TYPES = [
    "migration", "summarization", "classification", "qa",
    "generation", "review", "extraction", "translation",
    "refactoring", "documentation",
]

DOMAINS = [
    "code", "legal", "hr", "finance", "defense",
    "healthcare", "sales", "compliance", "customer-support", "other",
]

# Approximate VRAM usage (GB) for each model variant at 4-bit
_VRAM_ESTIMATES: dict[str, float] = {
    "7b":  6.0,
    "14b": 12.0,
    "70b": 48.0,
}


def _vram_estimate(name: str) -> float:
    for key, val in _VRAM_ESTIMATES.items():
        if key in name.lower():
            return val
    return 8.0


def list_base_models() -> list[dict[str, Any]]:
    models = []
    for path in sorted(MODELS_DIR.glob("*.yaml")):
        with path.open() as fh:
            data = yaml.safe_load(fh)
        models.append({
            "id":        path.stem,
            "name":      data.get("name", path.stem),
            "hf_path":   data.get("hf_path", ""),
            "format":    data.get("format", "raw"),
            "vram_gb":   _vram_estimate(data.get("name", path.stem)),
            "training":  data.get("training", {}),
            "inference": data.get("inference", {}),
        })
    return models


def get_base_model(model_id: str) -> dict[str, Any] | None:
    path = MODELS_DIR / f"{model_id}.yaml"
    if not path.exists():
        return None
    with path.open() as fh:
        data = yaml.safe_load(fh)
    data["id"] = path.stem
    data.setdefault("vram_gb", _vram_estimate(data.get("name", "")))
    return data


def list_project_configs() -> list[dict[str, Any]]:
    projects = []
    for path in sorted(PROJECTS_DIR.glob("*.yaml")):
        with path.open() as fh:
            data = yaml.safe_load(fh)
        data["id"] = path.stem
        projects.append(data)
    return projects


def get_project_config(project_id: str) -> dict[str, Any] | None:
    path = PROJECTS_DIR / f"{project_id}.yaml"
    if not path.exists():
        return None
    with path.open() as fh:
        data = yaml.safe_load(fh)
    data["id"] = project_id
    return data


def save_project_config(project_id: str, data: dict[str, Any]) -> None:
    path = PROJECTS_DIR / f"{project_id}.yaml"
    with path.open("w") as fh:
        yaml.dump(data, fh, allow_unicode=True, sort_keys=False)
