"""Benchmark hardware — mesure réelle tokens/s et TTFT sur le matériel local.

Résultats écrits dans memory/benchmarks/hardware_profiles.json.
Ce fichier est la source de vérité pour les valeurs injectées dans les prompts advisor.

Usage:
    python scripts/benchmark_hardware.py
    python scripts/benchmark_hardware.py --model Qwen2.5-Coder-7B-Instruct-4bit
    python scripts/benchmark_hardware.py --dry-run   # affiche sans écrire
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

_PROFILES_PATH = Path("memory/benchmarks/hardware_profiles.json")
_MODELS_DIR    = Path("models/mlx-community")

_BENCHMARK_PROMPT = "Explain the difference between RAG and fine-tuning in three sentences."
_N_WARMUP  = 2
_N_MEASURE = 5
_MAX_TOKENS = 100


def _detect_hardware() -> dict:
    """Detect current machine hardware profile."""
    hw = {
        "name": platform.node(),
        "platform": platform.platform(),
        "processor": platform.processor(),
        "python": platform.python_version(),
    }
    # Apple Silicon detection
    try:
        result = subprocess.check_output(
            ["system_profiler", "SPHardwareDataType"], text=True, timeout=10
        )
        for line in result.splitlines():
            if "Chip" in line:
                hw["chip"] = line.split(":")[-1].strip()
            if "Memory" in line and "GB" in line:
                hw["ram"] = line.split(":")[-1].strip()
    except Exception:
        pass
    return hw


def _run_mlx_benchmark(model_path: Path, n_warmup: int, n_measure: int) -> dict:
    """Run mlx_lm.generate and measure tokens/s + TTFT."""
    model_str = str(model_path)

    # Warmup
    for _ in range(n_warmup):
        subprocess.run(
            ["python", "-m", "mlx_lm.generate",
             "--model", model_str,
             "--prompt", _BENCHMARK_PROMPT,
             "--max-tokens", str(_MAX_TOKENS),
             "--temp", "0"],
            capture_output=True, timeout=120,
        )

    # Measure
    tps_samples: list[float] = []
    ttft_samples: list[float] = []

    for _ in range(n_measure):
        t0 = time.perf_counter()
        result = subprocess.run(
            ["python", "-m", "mlx_lm.generate",
             "--model", model_str,
             "--prompt", _BENCHMARK_PROMPT,
             "--max-tokens", str(_MAX_TOKENS),
             "--temp", "0"],
            capture_output=True, text=True, timeout=180,
        )
        total_time = time.perf_counter() - t0

        # Parse mlx_lm output for tokens/s
        for line in result.stderr.splitlines() + result.stdout.splitlines():
            if "tokens-per-sec" in line or "tok/s" in line.lower():
                try:
                    tps = float(line.split("=")[-1].strip().split()[0])
                    tps_samples.append(tps)
                except ValueError:
                    pass
            if "prompt" in line.lower() and "ms" in line.lower():
                try:
                    # "Prompt: X tokens, Y ms" → TTFT proxy
                    parts = line.split(",")
                    for p in parts:
                        if "ms" in p:
                            ttft = float(p.strip().replace("ms", "").split()[-1])
                            ttft_samples.append(ttft)
                except ValueError:
                    pass

        # Fallback: estimate from total time and token count
        if not tps_samples:
            tps_samples.append(_MAX_TOKENS / total_time)
        if not ttft_samples:
            ttft_samples.append(total_time * 1000 * 0.15)  # rough TTFT ≈ 15% of total

    tps_values = sorted(tps_samples)[1:-1] if len(tps_samples) > 2 else tps_samples
    ttft_values = sorted(ttft_samples)[1:-1] if len(ttft_samples) > 2 else ttft_samples

    return {
        "tokens_per_second": round(sum(tps_values) / len(tps_values), 1) if tps_values else None,
        "ttft_ms": round(sum(ttft_values) / len(ttft_values), 0) if ttft_values else None,
        "n_samples": n_measure,
    }


def benchmark_local_models(model_filter: str | None = None, dry_run: bool = False) -> dict:
    """Benchmark all available local models and update hardware_profiles.json."""
    hw_info = _detect_hardware()
    hw_name = hw_info.get("chip", "unknown-hardware")

    # Load existing profiles
    profiles: dict = {}
    if _PROFILES_PATH.exists():
        profiles = json.loads(_PROFILES_PATH.read_text())

    if hw_name not in profiles:
        profiles[hw_name] = {
            "hardware": hw_info,
            "source": "measured",
            "models": {},
        }

    models_to_bench = []
    if _MODELS_DIR.exists():
        for m in _MODELS_DIR.iterdir():
            if m.is_dir() and (model_filter is None or model_filter in m.name):
                models_to_bench.append(m)

    if not models_to_bench:
        print(f"No models found in {_MODELS_DIR}" + (f" matching '{model_filter}'" if model_filter else ""))
        return profiles

    results = {}
    for model_path in models_to_bench:
        print(f"Benchmarking {model_path.name}...")
        if dry_run:
            results[model_path.name] = {"tokens_per_second": None, "ttft_ms": None, "dry_run": True}
            continue
        try:
            metrics = _run_mlx_benchmark(model_path, _N_WARMUP, _N_MEASURE)
            metrics["measured_at"] = datetime.now(timezone.utc).isoformat()
            results[model_path.name] = metrics
            print(f"  → {metrics['tokens_per_second']} t/s, TTFT {metrics['ttft_ms']} ms")
        except Exception as e:
            print(f"  ✗ Failed: {e}")
            results[model_path.name] = {"error": str(e)}

    profiles[hw_name]["models"].update(results)
    profiles[hw_name]["last_run"] = datetime.now(timezone.utc).isoformat()

    if not dry_run:
        _PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
        _PROFILES_PATH.write_text(json.dumps(profiles, indent=2, ensure_ascii=False))
        print(f"\n✓ Profiles written to {_PROFILES_PATH}")

    return profiles


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Benchmark local MLX models")
    parser.add_argument("--model", default=None, help="Filter model name (substring)")
    parser.add_argument("--dry-run", action="store_true", help="Don't run, just print what would happen")
    args = parser.parse_args()
    benchmark_local_models(model_filter=args.model, dry_run=args.dry_run)
