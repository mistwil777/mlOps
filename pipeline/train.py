"""LoRA fine-tuning wrapper: builds the mlx_lm.lora command and logs to MLflow."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.logger import get_logger, log_metric, log_params, set_mlflow_run

logger = get_logger("mlops.train")

_MLX_PYTHON = os.environ.get("MLX_PYTHON", "python3.11")


def _load_yaml(path: Path | str) -> dict[str, Any]:
    with open(path) as fh:
        return yaml.safe_load(fh)


def _detect_available_ram_gb() -> float:
    """Return available physical RAM in GB."""
    try:
        import psutil  # noqa: PLC0415
        return psutil.virtual_memory().available / 1024**3
    except ImportError:
        return 16.0  # conservative default


def check_collection_gate(project_config: dict[str, Any]) -> None:
    """Raise RuntimeError if any adapter has not reached its minimum data threshold.

    Reads per-adapter JSONL files from processed/clean_raw/<adapter>.jsonl.
    Each adapter must have at least estimated_pairs * VOLUME_RATIO_LOW records.
    Training is blocked until ALL adapters pass.
    """
    project_id = project_config.get("id") or project_config.get("name", "").lower()
    adapters = project_config.get("adapters", [])
    clean_dir = Path("datalake/projects") / project_id / "processed" / "clean_raw"

    VOLUME_RATIO_LOW = 0.5  # mirrors clean_raw.py — must match

    failures: list[str] = []
    for adapter in adapters:
        name   = adapter["name"]
        target = adapter.get("estimated_pairs", 500)
        min_required = int(target * VOLUME_RATIO_LOW)
        jsonl_path = clean_dir / f"{name}.jsonl"

        if not jsonl_path.exists():
            failures.append(f"  {name}: file missing (0/{min_required} required)")
            continue

        count = sum(1 for line in jsonl_path.open() if line.strip())
        if count < min_required:
            failures.append(f"  {name}: {count}/{min_required} records (target={target})")

    if failures:
        msg = "\n".join(["Training blocked — adapters below minimum threshold:"] + failures)
        raise RuntimeError(msg)


class LoraTrainer:
    def __init__(self, project: str):
        self.project = project
        self.project_config = _load_yaml(Path("config/projects") / f"{project}.yaml")
        model_key = self.project_config.get("model", "mistral-7b-instruct-v0.3-4bit")
        # Map short names to config file names
        model_file_map = {
            "mistral-7b-instruct-v0.3-4bit": "mistral-7b",
            "qwen2.5-coder-14b-instruct-4bit": "qwen-14b",
        }
        config_file = model_file_map.get(model_key, model_key.split("-")[0] + "-7b")
        self.model_config = _load_yaml(Path("config/models") / f"{config_file}.yaml")

    def get_optimal_config(self, model_name: str, available_ram_gb: float) -> dict[str, Any]:
        """Return optimal training hyperparameters based on available RAM."""
        training = self.model_config.get("training", {})
        config = dict(training)

        if available_ram_gb < 24:
            config["batch_size"] = max(1, config.get("batch_size", 4) // 2)
            config["lora_layers"] = min(8, config.get("lora_layers", 16))
            logger.warning("Low RAM (%.1f GB) — reduced batch_size=%d lora_layers=%d",
                           available_ram_gb, config["batch_size"], config["lora_layers"])
        return config

    def build_command(
        self,
        model: str,
        data_path: str,
        config: dict[str, Any],
        adapter_path: str,
        resume: str | None = None,
    ) -> str:
        hf_path = self.model_config.get("hf_path", model)
        parts = [
            _MLX_PYTHON, "-m", "mlx_lm.lora",
            "--model", hf_path,
            "--train",
            "--data", data_path,
            "--batch-size", str(config.get("batch_size", 4)),
            "--lora-layers", str(config.get("lora_layers", 16)),
            "--iters", str(config.get("iters", 1000)),
            "--learning-rate", str(config.get("learning_rate", 1e-4)),
            "--steps-per-eval", str(config.get("steps_per_eval", 100)),
            "--val-batches", str(config.get("val_batches", 25)),
            "--save-every", str(config.get("save_every", 200)),
            "--adapter-path", adapter_path,
        ]
        if resume:
            parts += ["--resume-adapter-file", resume]
        return " ".join(parts)

    def run(
        self,
        experiment_name: str,
        tags: dict[str, str] | None = None,
        iters: int | None = None,
        resume: str | None = None,
    ) -> str:
        import mlflow  # noqa: PLC0415

        mlflow.set_tracking_uri(os.environ.get("MLFLOW_TRACKING_URI", "http://localhost:5001"))
        mlflow.set_experiment(experiment_name)

        check_collection_gate(self.project_config)

        available_ram = _detect_available_ram_gb()
        config = self.get_optimal_config(self.project_config["model"], available_ram)
        if iters is not None:
            config["iters"] = iters

        with mlflow.start_run(tags=tags or {}) as run:
            run_id = run.info.run_id
            set_mlflow_run(run_id)

            adapter_path = str(Path("adapters") / self.project / run_id)
            data_path = str(Path("datalake/projects") / self.project / "fine-tuning-ready")

            mlflow.log_params({
                "project": self.project,
                "model": self.project_config["model"],
                "available_ram_gb": round(available_ram, 1),
                **{f"train_{k}": v for k, v in config.items()},
            })

            cmd = self.build_command(
                model=self.project_config["model"],
                data_path=data_path,
                config=config,
                adapter_path=adapter_path,
                resume=resume,
            )
            logger.info("Training command: %s", cmd)

            process = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            for line in process.stdout:  # type: ignore[union-attr]
                line = line.rstrip()
                print(line, flush=True)
                # Parse loss lines: "Iter N: train loss X, val loss Y"
                m = re.search(r"Iter\s+(\d+).*train loss\s+([\d.]+)(?:.*val loss\s+([\d.]+))?", line)
                if m:
                    step = int(m.group(1))
                    mlflow.log_metric("train_loss", float(m.group(2)), step=step)
                    if m.group(3):
                        mlflow.log_metric("val_loss", float(m.group(3)), step=step)

            process.wait()
            if process.returncode != 0:
                mlflow.set_tag("status", "FAILED")
                raise RuntimeError(f"Training failed with exit code {process.returncode}")

            mlflow.log_artifacts(adapter_path, artifact_path="adapter")
            mlflow.set_tag("adapter_path", adapter_path)
            mlflow.set_tag("status", "COMPLETED")

        return run_id


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", required=True)
    parser.add_argument("--experiment", default=None)
    parser.add_argument("--iters", type=int, default=None)
    parser.add_argument("--resume", default=None)
    args = parser.parse_args()

    trainer = LoraTrainer(args.project)
    run_id = trainer.run(
        experiment_name=args.experiment or args.project,
        iters=args.iters,
        resume=args.resume,
    )
    print(f"Training completed. MLflow run_id: {run_id}")


if __name__ == "__main__":
    main()
