"""Model deployment: fuse adapter, run sanity check, serve, rollback."""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import yaml

from pipeline.utils.logger import get_logger

logger = get_logger("mlops.deploy")

_MLX_PYTHON = os.environ.get("MLX_PYTHON", "python3.11")


def _load_project_config(project: str) -> dict[str, Any]:
    path = Path("config/projects") / f"{project}.yaml"
    with path.open() as fh:
        return yaml.safe_load(fh)


def _save_project_config(project: str, config: dict[str, Any]) -> None:
    path = Path("config/projects") / f"{project}.yaml"
    with path.open("w") as fh:
        yaml.dump(config, fh, default_flow_style=False, allow_unicode=True)


class ModelDeployer:
    def __init__(self, project: str):
        self.project = project
        self.config = _load_project_config(project)

    def fuse(self, base_model: str, adapter_path: str, output_path: str) -> bool:
        """Fuse a LoRA adapter into the base model weights."""
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
            return False
        logger.info("Fuse complete → %s", output_path)
        return True

    def sanity_check(self, model_path: str, n_requests: int = 10) -> tuple[bool, dict[str, Any]]:
        """Run n_requests against the serving model and check compilability."""
        import httpx  # noqa: PLC0415
        from pipeline.utils.metrics import compile_java  # noqa: PLC0415

        test_prompts = [
            "Convert this Python to Java:\n\ndef add(a, b):\n    return a + b",
            "Convert this Python to Java:\n\ndef is_even(n):\n    return n % 2 == 0",
        ]

        port = self.config.get("serving_port", 8080)
        results = []
        latencies = []
        ok_count = 0

        for i in range(n_requests):
            prompt = test_prompts[i % len(test_prompts)]
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
                compiled, _ = compile_java(completion)
                if compiled:
                    ok_count += 1
                results.append({"compiled": compiled, "latency_ms": latency})
            except Exception as exc:  # noqa: BLE001
                logger.error("Request %d failed: %s", i, exc)
                results.append({"compiled": False, "error": str(exc)})

        success = ok_count >= n_requests * 0.8  # 80% pass rate for sanity
        summary = {
            "n_requests": n_requests,
            "compiled": ok_count,
            "success_rate": ok_count / n_requests,
            "latency_mean_ms": sum(latencies) / len(latencies) if latencies else 0,
            "passed": success,
        }
        logger.info("Sanity check: %d/%d compiled, mean latency=%.0fms",
                    ok_count, n_requests, summary["latency_mean_ms"])
        return success, summary

    def serve(self, model_path: str, port: int = 8080) -> subprocess.Popen:
        """Start mlx_lm.server as a background process."""
        pid_file = Path("models") / self.project / "server.pid"
        pid_file.parent.mkdir(parents=True, exist_ok=True)

        # Stop existing server on that port
        if pid_file.exists():
            old_pid = pid_file.read_text().strip()
            subprocess.run(["kill", old_pid], capture_output=True)
            logger.info("Stopped previous server (PID %s)", old_pid)

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
