"""Model deployment: fuse adapter, run sanity check, serve, rollback."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.logger import get_logger

logger = get_logger("mlops.deploy")

_MLX_PYTHON = os.environ.get("MLX_PYTHON", "python3.11")

# mlx_lm.server is brew-installed — prefer the system binary over a venv
_MLX_SERVER_BIN = os.environ.get("MLX_SERVER_BIN", "mlx_lm.server")


def _load_project_config(project: str) -> dict[str, Any]:
    path = Path("config/projects") / f"{project}.yaml"
    with path.open() as fh:
        return yaml.safe_load(fh)


def _save_project_config(project: str, config: dict[str, Any]) -> None:
    path = Path("config/projects") / f"{project}.yaml"
    with path.open("w") as fh:
        yaml.dump(config, fh, default_flow_style=False, allow_unicode=True)


def _sanity_prompts(domain: str, task_type: str) -> list[str]:
    """Return a small set of generic sanity prompts for this domain/task_type."""
    prompts: dict[str, list[str]] = {
        "migration": [
            "Migrate this Python function to Java:\n\ndef add(a, b):\n    return a + b",
            "Migrate this Python function to Java:\n\ndef is_empty(lst):\n    return len(lst) == 0",
        ],
        "summarization": [
            "Summarize the following paragraph in one sentence:\n\n"
            "Machine learning is a branch of artificial intelligence that enables systems "
            "to learn from data and improve over time without explicit programming.",
            "Summarize in 2 sentences:\n\nThe Eiffel Tower was built in 1889 as the entrance arch "
            "for the World's Fair held in Paris. It stands 330 meters tall.",
        ],
        "documentation": [
            "Write a docstring for:\n\ndef multiply(a: int, b: int) -> int:\n    return a * b",
            "Write a docstring for:\n\ndef filter_even(lst: list[int]) -> list[int]:\n    return [x for x in lst if x % 2 == 0]",
        ],
        "qa": [
            "Answer the question based on the context.\nContext: Paris is the capital of France.\nQuestion: What is the capital of France?",
            "Answer the question.\nContext: Python was created by Guido van Rossum in 1991.\nQuestion: Who created Python?",
        ],
    }
    default = [
        f"Perform a {task_type} task on the following input:\n\nHello, world.",
        f"Apply {domain} {task_type} to the following:\n\nfoo(bar, baz)",
    ]
    return prompts.get(task_type, default)


class ModelDeployer:
    def __init__(self, project: str):
        self.project = project
        self.config = _load_project_config(project)

    def fuse(self, base_model: str, adapter_path: str, model_id: str) -> tuple[bool, str]:
        """Fuse a LoRA adapter into the base model weights → models/staging/<project>/<model_id>.

        Returns (success, staging_path).
        """
        output_path = str(Path("models/staging") / self.project / model_id)
        Path(output_path).mkdir(parents=True, exist_ok=True)
        cmd = [
            _MLX_PYTHON, "-m", "mlx_lm.fuse",
            "--model", base_model,
            "--adapter-path", adapter_path,
            "--save-path", output_path,
        ]
        logger.info("Fusing: %s", " ".join(cmd))
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error("Fuse failed: %s", result.stderr)
            return False, output_path
        logger.info("Fuse complete → %s (staging)", output_path)
        return True, output_path

    def gate_and_promote(self, mlflow_run_id: str, model_id: str, force: bool = False) -> tuple[bool, str]:
        """Run deploy gate then promote staging → production.

        Returns (promoted, production_path).
        """
        from scripts.deploy_gate import promote, run_gate  # noqa: PLC0415

        gate_result_path = f"logs/gate_{self.project}_{model_id}.json"
        passed = run_gate(self.project, mlflow_run_id, gate_result_path)

        if not passed and not force:
            logger.error("Deploy gate FAILED for %s — use force=True to override", self.project)
            return False, ""

        if not passed and force:
            logger.warning("Deploy gate FAILED but force=True — promoting anyway")

        promote(self.project, model_id)
        production_path = str(Path("models/production") / self.project / model_id)
        return True, production_path

    def sanity_check(self, model_path: str, n_requests: int = 10) -> tuple[bool, dict[str, Any]]:
        """Run n_requests against the serving model and check response validity."""
        import httpx  # noqa: PLC0415
        from pipeline.utils.metrics import rouge_l_score  # noqa: PLC0415

        domain    = self.config.get("domain", "code")
        task_type = self.config.get("task_type", "generation")
        prompts   = _sanity_prompts(domain, task_type)

        port = self.config.get("serving_port", 8080)
        results = []
        latencies = []
        ok_count = 0

        for i in range(n_requests):
            prompt = prompts[i % len(prompts)]
            t0 = time.perf_counter()
            try:
                resp = httpx.post(
                    f"http://localhost:{port}/v1/chat/completions",
                    json={
                        "model": "local",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 256,
                        "temperature": 0.1,
                    },
                    timeout=30.0,
                )
                latency = (time.perf_counter() - t0) * 1000
                latencies.append(latency)
                completion = resp.json()["choices"][0]["message"]["content"]
                valid = bool(completion and len(completion.strip()) > 5)
                if valid:
                    ok_count += 1
                results.append({"valid": valid, "latency_ms": latency})
            except Exception as exc:  # noqa: BLE001
                logger.error("Request %d failed: %s", i, exc)
                results.append({"valid": False, "error": str(exc)})

        success = ok_count >= n_requests * 0.8
        summary = {
            "n_requests": n_requests,
            "valid_responses": ok_count,
            "success_rate": ok_count / n_requests,
            "latency_mean_ms": sum(latencies) / len(latencies) if latencies else 0,
            "passed": success,
        }
        logger.info("Sanity check: %d/%d valid, mean latency=%.0fms",
                    ok_count, n_requests, summary["latency_mean_ms"])
        return success, summary

    def serve(self, model_path: str, port: int = 8080) -> subprocess.Popen:
        """Start mlx_lm.server — only accepts paths under models/production/."""
        model_path_obj = Path(model_path).resolve()
        production_root = Path("models/production").resolve()
        if not str(model_path_obj).startswith(str(production_root)):
            raise ValueError(
                f"Refusing to serve from outside production: {model_path}. "
                "Run gate_and_promote() first."
            )
        pid_file = Path("models") / self.project / "server.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)

        if pid_file.exists():
            old_pid = pid_file.read_text().strip()
            subprocess.run(["kill", old_pid], capture_output=True)
            logger.info("Stopped previous server (PID %s)", old_pid)

        # Try brew binary first, fall back to python -m mlx_lm.server
        try:
            import shutil  # noqa: PLC0415
            bin_path = shutil.which(_MLX_SERVER_BIN)
            if bin_path:
                cmd = [bin_path, "--model", model_path, "--port", str(port)]
            else:
                cmd = [_MLX_PYTHON, "-m", "mlx_lm.server", "--model", model_path, "--port", str(port)]
        except Exception:
            cmd = [_MLX_PYTHON, "-m", "mlx_lm.server", "--model", model_path, "--port", str(port)]

        process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pid_file.write_text(str(process.pid))
        logger.info("Server started on port %d (PID %d)", port, process.pid)
        return process

    def update_config(self, project: str, new_model_path: str) -> None:
        """Update project config with new model path, preserve rollback pointer."""
        config = _load_project_config(project)
        old_path = config.get("model_path")
        if old_path:
            config["previous_model_path"] = old_path

        config["model_path"] = new_model_path
        from datetime import datetime  # noqa: PLC0415
        config["deployed_at"] = datetime.utcnow().isoformat()

        _save_project_config(project, config)
        logger.info("Config updated: %s → %s", project, new_model_path)

    def rollback(self, project: str) -> bool:
        """Restart server with the previous model version."""
        config = _load_project_config(project)
        previous = config.get("previous_model_path")
        if not previous:
            logger.error("No rollback target found in config for %s", project)
            return False

        logger.info("Rolling back %s to %s", project, previous)
        port = config.get("serving_port", 8080)
        self.serve(previous, port)
        self.update_config(project, previous)
        return True
