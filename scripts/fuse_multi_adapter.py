"""Fuse multiple LoRA adapters into one model for embedded/sovereign deployment.

Each adapter is fused sequentially. The base model is loaded once; each adapter
is applied via mlx_lm.fuse and the output becomes the base for the next adapter.
Final fused model lands in models/staging/<project>/fused-<adapters_hash>/
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path


def _adapter_path(project: str, adapter_name: str) -> Path:
    return Path("adapters") / project / adapter_name


def _fuse_one(base_model: str, adapter_path: Path, output_path: Path) -> None:
    """Run mlx_lm.fuse for a single adapter."""
    cmd = [
        sys.executable, "-m", "mlx_lm.fuse",
        "--model", base_model,
        "--adapter-path", str(adapter_path),
        "--save-path", str(output_path),
        "--de-quantize",  # fuse into fp16 for subsequent adapter application
    ]
    print(f"  fusing {adapter_path.name} → {output_path}")
    result = subprocess.run(cmd, capture_output=False)
    if result.returncode != 0:
        raise RuntimeError(f"mlx_lm.fuse failed for adapter {adapter_path.name}")


def fuse_multi_adapter(
    project: str,
    adapter_names: list[str],
    base_model: str | None = None,
    output_name: str | None = None,
) -> Path:
    """Fuse N adapters sequentially.

    If base_model is None, reads it from config/projects/<project>.yaml.
    Returns the staging output path.
    """
    import yaml

    if base_model is None:
        cfg_path = Path("config/projects") / f"{project}.yaml"
        if not cfg_path.exists():
            raise FileNotFoundError(f"Project config not found: {cfg_path}")
        cfg = yaml.safe_load(cfg_path.read_text())
        base_model = cfg.get("model")
        if not base_model:
            raise ValueError(f"No 'model' field in {cfg_path}")

    for name in adapter_names:
        p = _adapter_path(project, name)
        if not p.exists():
            raise FileNotFoundError(f"Adapter not found: {p}")

    # Deterministic output name based on adapter list
    if output_name is None:
        digest = hashlib.sha1("+".join(sorted(adapter_names)).encode()).hexdigest()[:8]
        output_name = f"fused-{digest}"

    staging_dir = Path("models/staging") / project / output_name
    staging_dir.parent.mkdir(parents=True, exist_ok=True)

    print(f"Fusing {len(adapter_names)} adapters for project={project}")
    print(f"  Base model  : {base_model}")
    print(f"  Adapters    : {', '.join(adapter_names)}")
    print(f"  Output      : {staging_dir}")

    current_base = base_model
    tmp_dirs: list[Path] = []

    for i, adapter_name in enumerate(adapter_names):
        adapter_path = _adapter_path(project, adapter_name)
        is_last = i == len(adapter_names) - 1
        out = staging_dir if is_last else staging_dir.parent / f"_tmp_{i}_{adapter_name}"

        _fuse_one(current_base, adapter_path, out)
        if not is_last:
            tmp_dirs.append(out)
            current_base = str(out)

    # Cleanup intermediate fusions
    import shutil
    for tmp in tmp_dirs:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"Done — fused model at {staging_dir}")
    print("Run deploy_gate.py check then promote to production before serving.")
    return staging_dir


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Fuse multiple LoRA adapters into one model")
    parser.add_argument("--project", required=True)
    parser.add_argument("--adapters", required=True, help="Comma-separated adapter names")
    parser.add_argument("--base-model", help="Override base model path/ID")
    parser.add_argument("--output", help="Output name under models/staging/<project>/")
    args = parser.parse_args()

    adapter_names = [a.strip() for a in args.adapters.split(",") if a.strip()]
    if not adapter_names:
        print("ERROR: --adapters must list at least one adapter name")
        sys.exit(1)

    fuse_multi_adapter(
        project=args.project,
        adapter_names=adapter_names,
        base_model=args.base_model,
        output_name=args.output,
    )


if __name__ == "__main__":
    main()
