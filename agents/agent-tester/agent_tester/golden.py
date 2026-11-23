"""Level 3 — Behavioral golden tests (inspect-ai).

Optional — requires a real LLM and inspect-ai installed.
Skipped automatically if inspect-ai is not available or no LLM is configured.

Golden tests check that the agent produces structurally correct outputs
on known reference inputs. They compare structure, not exact text.

Usage:
    from agent_tester.golden import run_golden_tests
    report = run_golden_tests(Path("agents/pair-forge"), "pair_forge", llm_config={...})
    # llm_config = {"provider": "bedrock", "model_id": "...", "temperature": 0.1}
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GoldenTestCase:
    name: str
    passed: bool
    skipped: bool = False
    error: str | None = None


@dataclass
class GoldenReport:
    agent_name: str
    passed: bool
    skipped: bool
    tests: list[GoldenTestCase] = field(default_factory=list)

    @property
    def total(self)   -> int: return len(self.tests)
    @property
    def n_skipped(self) -> int: return sum(1 for t in self.tests if t.skipped)
    @property
    def failed(self)  -> int: return sum(1 for t in self.tests if not t.passed and not t.skipped)


def run_golden_tests(
    agent_path: Path,
    pkg_name: str,
    llm_config: dict | None = None,
) -> GoldenReport:
    """Run behavioral golden tests. Returns a skipped report if inspect-ai or LLM unavailable."""
    if not _inspect_ai_available():
        return GoldenReport(
            pkg_name, passed=True, skipped=True,
            tests=[GoldenTestCase(
                "inspect-ai availability", passed=True, skipped=True,
                error="inspect-ai not installed — install with: pip install inspect-ai",
            )],
        )

    if not llm_config:
        return GoldenReport(
            pkg_name, passed=True, skipped=True,
            tests=[GoldenTestCase(
                "llm config", passed=True, skipped=True,
                error="No LLM config provided — pass llm_config={provider, model_id} to enable",
            )],
        )

    path = Path(agent_path).resolve()
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

    tests: list[GoldenTestCase] = []
    tests.append(_test_pair_generation(pkg_name, llm_config, agent_path))
    tests.append(_test_judge_quality(pkg_name, llm_config, agent_path))

    passed = all(t.passed or t.skipped for t in tests)
    return GoldenReport(pkg_name, passed=passed, skipped=False, tests=tests)


# ── Individual golden tasks ────────────────────────────────────────────────────

def _test_pair_generation(pkg_name: str, llm_config: dict, agent_path: Path) -> GoldenTestCase:
    name = "pair_generation_produces_valid_pairs"
    try:
        from inspect_ai import Task, task  # noqa: PLC0415
        from inspect_ai.dataset import Sample  # noqa: PLC0415
        from inspect_ai.scorer import Score, scorer  # noqa: PLC0415
        from inspect_ai.solver import Generate, solver  # noqa: PLC0415

        import importlib  # noqa: PLC0415
        mod       = importlib.import_module(pkg_name)
        agent_cls = next(
            getattr(mod, n) for n in dir(mod)
            if "Agent" in n and not n.startswith("_")
        )

        config = {
            "domain": "test", "task_type": "generation",
            "adapters": [{"name": "default", "description": ""}],
            "ingestion": {
                "pair_generator": llm_config,
                "pair_judge": {**llm_config, "min_score": 0.5, "auto_correct": False, "max_retries": 0},
            },
            "min_tokens": 10, "max_tokens": 512,
        }

        agent    = agent_cls(project_id="golden_test", config=config)
        fixture  = agent_path / pkg_name / "tests" / "smoke" / "fixtures" / "reference.pdf"

        if not fixture.exists():
            return GoldenTestCase(name, passed=True, skipped=True,
                                  error="No reference.pdf fixture found")

        results = list(agent.run(fixture, source="golden_test"))
        summary = next((r for r in results if r.get("summary")), None)

        if summary is None:
            return GoldenTestCase(name, False, error="No summary dict yielded by agent.run()")

        if summary.get("generated", 0) == 0:
            return GoldenTestCase(name, False,
                                  error=f"Agent generated 0 pairs. Full summary: {summary}")

        return GoldenTestCase(name, True)

    except Exception as exc:  # noqa: BLE001
        return GoldenTestCase(name, False, error=str(exc))


def _test_judge_quality(pkg_name: str, llm_config: dict, agent_path: Path) -> GoldenTestCase:
    name = "judge_scores_sensible_pair_above_threshold"
    try:
        import importlib  # noqa: PLC0415
        mod       = importlib.import_module(pkg_name)
        agent_cls = next(
            getattr(mod, n) for n in dir(mod)
            if "Agent" in n and not n.startswith("_")
        )

        config = {
            "domain": "test", "task_type": "generation",
            "adapters": [{"name": "default", "description": ""}],
            "ingestion": {
                "pair_generator": llm_config,
                "pair_judge": {**llm_config, "min_score": 0.5, "auto_correct": False, "max_retries": 0},
            },
            "min_tokens": 10, "max_tokens": 512,
        }
        agent = agent_cls(project_id="golden_test", config=config)

        # A clearly correct pair should score above 0.5
        good_pair = {
            "input":  "Summarise the following clause and identify the main obligation.",
            "output": "The clause establishes a confidentiality obligation binding the contractor for 5 years.",
        }
        verdict = agent._judge_pair(good_pair, {"name": "default", "description": ""})
        score   = verdict.get("score", 0.0)

        if score < 0.5:
            return GoldenTestCase(
                name, False,
                error=f"Expected score >= 0.5 for a correct pair, got {score:.2f}",
            )
        return GoldenTestCase(name, True)

    except Exception as exc:  # noqa: BLE001
        return GoldenTestCase(name, False, error=str(exc))


def _inspect_ai_available() -> bool:
    try:
        import inspect_ai  # noqa: F401, PLC0415
        return True
    except ImportError:
        return False
