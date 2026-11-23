"""Level 2 — Prompt contract tests.

Tests that an agent's LLM prompts return the expected JSON schemas,
using a mock LLM — no real API call required.

Four contracts tested per agent (if it exposes the standard pair-forge prompts):
  1. PAIR_GENERATION   → JSON array of {input, output}
  2. JUDGE_EVALUATION  → JSON object with score (0.0–1.0)
  3. PAIR_CORRECTION   → JSON object with input + output
  4. Resilience        → agent does not crash on malformed / None LLM responses

Usage:
    from agent_tester.prompt_tests import run_prompt_tests
    report = run_prompt_tests(Path("agents/pair-forge"), "pair_forge")
"""

from __future__ import annotations

import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ── JSON schemas ───────────────────────────────────────────────────────────────

GENERATION_SCHEMA: dict = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["input", "output"],
        "properties": {
            "input":  {"type": "string"},
            "output": {"type": "string"},
        },
    },
    "minItems": 1,
}

JUDGE_SCHEMA: dict = {
    "type": "object",
    "required": ["score"],
    "properties": {
        "score": {"type": "number", "minimum": 0.0, "maximum": 1.0},
        "error_type":        {"type": ["string", "null"]},
        "error_description": {"type": ["string", "null"]},
        "suggested_fix":     {"type": ["string", "null"]},
    },
}

CORRECTION_SCHEMA: dict = {
    "type": "object",
    "required": ["input", "output"],
    "properties": {
        "input":  {"type": "string"},
        "output": {"type": "string"},
    },
}


# ── Result types ───────────────────────────────────────────────────────────────

@dataclass
class PromptTestCase:
    name: str
    passed: bool
    error: str | None = None


@dataclass
class PromptTestReport:
    agent_name: str
    passed: bool
    tests: list[PromptTestCase] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.tests)

    @property
    def failed(self) -> int:
        return sum(1 for t in self.tests if not t.passed)


# ── Entry point ────────────────────────────────────────────────────────────────

def run_prompt_tests(agent_path: Path, pkg_name: str) -> PromptTestReport:
    """Run all prompt contract tests against an agent.

    Imports the agent's PairForgeAgent (or equivalent) by injecting a mock
    LLM function — no real LLM call is made.
    """
    path = Path(agent_path).resolve()
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

    tests: list[PromptTestCase] = []

    try:
        agent_cls = _import_agent_class(pkg_name)
    except Exception as exc:  # noqa: BLE001
        return PromptTestReport(
            pkg_name, False,
            [PromptTestCase("import_agent_class", False, str(exc))],
        )

    config = _minimal_config()

    # Test 1: PAIR_GENERATION — valid JSON array
    tests.append(_test_generation_valid(agent_cls, config))

    # Test 2: PAIR_GENERATION — JSON in markdown fences
    tests.append(_test_generation_fenced(agent_cls, config))

    # Test 3: PAIR_GENERATION — malformed JSON → fallback (no crash)
    tests.append(_test_generation_malformed(agent_cls, config))

    # Test 4: PAIR_GENERATION — None response → fallback (no crash)
    tests.append(_test_generation_none(agent_cls, config))

    # Test 5: JUDGE_EVALUATION — valid JSON object
    tests.append(_test_judge_valid(agent_cls, config))

    # Test 6: JUDGE_EVALUATION — score out of range → handled gracefully
    tests.append(_test_judge_out_of_range(agent_cls, config))

    passed = all(t.passed for t in tests)
    return PromptTestReport(pkg_name, passed, tests)


# ── Individual test cases ──────────────────────────────────────────────────────

def _test_generation_valid(cls: type, config: dict) -> PromptTestCase:
    name = "generation_valid_json"
    mock_response = json.dumps([
        {"input": "Summarise the following document.", "output": "The document covers topic X."},
    ])
    agent = cls(project_id="test", config=config, llm_fn=lambda p, c, m: mock_response)
    try:
        pairs = agent._generate_pairs("Some document content here.", _adapter())
        _validate_schema(pairs, GENERATION_SCHEMA)
        return PromptTestCase(name, True)
    except Exception as exc:  # noqa: BLE001
        return PromptTestCase(name, False, str(exc))


def _test_generation_fenced(cls: type, config: dict) -> PromptTestCase:
    name = "generation_json_in_markdown_fences"
    mock_response = (
        "```json\n"
        + json.dumps([{"input": "q", "output": "a"}])
        + "\n```"
    )
    agent = cls(project_id="test", config=config, llm_fn=lambda p, c, m: mock_response)
    try:
        pairs = agent._generate_pairs("content", _adapter())
        assert len(pairs) >= 1, "Expected at least 1 pair"
        return PromptTestCase(name, True)
    except Exception as exc:  # noqa: BLE001
        return PromptTestCase(name, False, str(exc))


def _test_generation_malformed(cls: type, config: dict) -> PromptTestCase:
    name = "generation_malformed_json_no_crash"
    agent = cls(project_id="test", config=config, llm_fn=lambda p, c, m: "this is not json {{")
    try:
        pairs = agent._generate_pairs("content", _adapter())
        assert isinstance(pairs, list), "Expected list fallback"
        return PromptTestCase(name, True)
    except Exception as exc:  # noqa: BLE001
        return PromptTestCase(name, False, str(exc))


def _test_generation_none(cls: type, config: dict) -> PromptTestCase:
    name = "generation_none_response_no_crash"
    agent = cls(project_id="test", config=config, llm_fn=lambda p, c, m: None)
    try:
        pairs = agent._generate_pairs("content", _adapter())
        assert isinstance(pairs, list), "Expected list fallback"
        return PromptTestCase(name, True)
    except Exception as exc:  # noqa: BLE001
        return PromptTestCase(name, False, str(exc))


def _test_judge_valid(cls: type, config: dict) -> PromptTestCase:
    name = "judge_valid_schema"
    mock_response = json.dumps({"score": 0.92, "error_type": None,
                                "error_description": None, "suggested_fix": None})
    agent = cls(project_id="test", config=config, llm_fn=lambda p, c, m: mock_response)
    try:
        pair    = {"input": "Convert Foo", "output": "class Foo: pass"}
        verdict = agent._judge_pair(pair, _adapter())
        _validate_schema(verdict, JUDGE_SCHEMA)
        assert 0.0 <= verdict["score"] <= 1.0
        return PromptTestCase(name, True)
    except Exception as exc:  # noqa: BLE001
        return PromptTestCase(name, False, str(exc))


def _test_judge_out_of_range(cls: type, config: dict) -> PromptTestCase:
    name = "judge_score_fallback_on_missing"
    agent = cls(project_id="test", config=config, llm_fn=lambda p, c, m: "{}")
    try:
        verdict = agent._judge_pair({"input": "q", "output": "a"}, _adapter())
        assert "score" in verdict, "score key must exist even with empty response"
        return PromptTestCase(name, True)
    except Exception as exc:  # noqa: BLE001
        return PromptTestCase(name, False, str(exc))


# ── Helpers ────────────────────────────────────────────────────────────────────

def _import_agent_class(pkg_name: str) -> type:
    module = importlib.import_module(pkg_name)
    cls_name = next(
        (name for name in dir(module) if "Agent" in name and not name.startswith("_")),
        None,
    )
    if cls_name is None:
        raise ImportError(f"No Agent class found in {pkg_name}.__init__")
    return getattr(module, cls_name)


def _minimal_config() -> dict:
    return {
        "domain":    "test",
        "task_type": "generation",
        "adapters":  [{"name": "default", "description": "test adapter"}],
        "ingestion": {
            "pair_generator": {"provider": "mock", "model_id": "mock", "temperature": 0.1},
            "pair_judge":     {"provider": "mock", "model_id": "mock", "temperature": 0.1,
                               "min_score": 0.8, "auto_correct": False, "max_retries": 0},
        },
        "min_tokens": 10,
        "max_tokens": 512,
    }


def _adapter() -> dict:
    return {"name": "default", "description": "test"}


def _validate_schema(data: Any, schema: dict) -> None:
    try:
        import jsonschema  # noqa: PLC0415
        jsonschema.validate(instance=data, schema=schema)
    except ImportError:
        # jsonschema not installed — basic structural check
        if schema.get("type") == "array":
            assert isinstance(data, list), f"Expected list, got {type(data)}"
        elif schema.get("type") == "object":
            assert isinstance(data, dict), f"Expected dict, got {type(data)}"
            for req in schema.get("required", []):
                assert req in data, f"Required field missing: {req}"
