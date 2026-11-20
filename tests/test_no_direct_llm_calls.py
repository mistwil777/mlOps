"""Guard: every text LLM call goes through pipeline/utils/llm.py with a project_id (rule 14 in CLAUDE.md).

Two ratchets (standard library only). tests/llm_gateway_baseline.json lists the existing debt per file and
may only shrink (`python tests/test_no_direct_llm_calls.py --shrink`, which refuses to raise it):
  - direct provider calls outside llm.py (boto3 bedrock-runtime, anthropic SDK): they bypass the policy
  - call_llm / call_llm_with_config calls without project_id: they are audited as "unscoped"
"""

import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BASELINE = Path(__file__).with_name("llm_gateway_baseline.json")
DIRS = ("pipeline", "agents", "api", "scripts")
GATEWAY = "pipeline/utils/llm.py"
DIRECT = re.compile(
    r"boto3\.client\(\s*[\"']bedrock-runtime[\"']|anthropic\.(?:Anthropic|AnthropicBedrock)\(|"
    r"\bAnthropic(?:Bedrock)?\(\s*(?:api_key|\))"
)
GATEWAY_FUNCS = {"call_llm", "call_llm_with_config"}


def _python_files():
    for d in DIRS:
        for path in sorted((ROOT / d).rglob("*.py")):
            yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")


def _direct_calls() -> dict[str, int]:
    out = {}
    for rel, text in _python_files():
        if rel == GATEWAY:
            continue
        n = len(DIRECT.findall(text))
        if n:
            out[rel] = n
    return out


def _unscoped_calls() -> dict[str, int]:
    out = {}
    for rel, text in _python_files():
        if rel == GATEWAY:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        n = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
                if name in GATEWAY_FUNCS and not any(k.arg == "project_id" for k in node.keywords):
                    n += 1
        if n:
            out[rel] = n
    return out


def _current() -> dict[str, dict[str, int]]:
    return {"direct_provider_calls": _direct_calls(), "calls_without_project_id": _unscoped_calls()}


def _baseline() -> dict[str, dict[str, int]]:
    return json.loads(BASELINE.read_text(encoding="utf-8")) if BASELINE.exists() else {}


def _worse(kind: str) -> list[str]:
    base = _baseline().get(kind, {})
    return [
        f"{rel}: {n} (baseline {base.get(rel, 0)})"
        for rel, n in sorted(_current()[kind].items())
        if n > base.get(rel, 0)
    ]


def test_no_new_direct_provider_calls() -> None:
    worse = _worse("direct_provider_calls")
    assert not worse, (
        "Direct LLM provider call(s) outside pipeline/utils/llm.py - they bypass the project policy and the audit.\n"
        "Use call_llm(..., project_id=...):\n" + "\n".join(worse)
    )


def test_no_new_calls_without_project_id() -> None:
    worse = _worse("calls_without_project_id")
    assert not worse, (
        "call_llm without project_id - the call is audited as 'unscoped' and its project policy is not applied:\n"
        + "\n".join(worse)
    )


def shrink() -> int:
    base, current = _baseline(), _current()
    first_run = not BASELINE.exists()
    for kind, counts in current.items():
        if not first_run and any(n > base.get(kind, {}).get(rel, 0) for rel, n in counts.items()):
            print(f"Refusing to raise the baseline ({kind}): fix the new occurrences first.")
            return 1
    BASELINE.write_text(
        json.dumps({k: dict(sorted(v.items())) for k, v in current.items()}, indent=2) + "\n", encoding="utf-8"
    )
    print({k: f"{sum(v.values())} in {len(v)} file(s)" for k, v in current.items()})
    return 0


if __name__ == "__main__":
    if "--shrink" not in sys.argv:
        sys.exit("usage: python tests/test_no_direct_llm_calls.py --shrink")
    sys.exit(shrink())
