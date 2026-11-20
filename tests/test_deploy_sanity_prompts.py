"""Sanity prompts must come from the project config first, never from a hardcoded domain."""

from pipeline.deploy import _sanity_prompts


def test_configured_prompts_take_priority() -> None:
    assert _sanity_prompts("any", "qa", ["custom prompt"]) == ["custom prompt"]


def test_generic_prompts_by_task_type() -> None:
    prompts = _sanity_prompts("any", "summarization")
    assert prompts and all("Summarize" in p for p in prompts)


def test_unknown_task_type_falls_back_to_generic() -> None:
    prompts = _sanity_prompts("legal", "extraction")
    assert len(prompts) == 2 and all("extraction" in p for p in prompts)


def test_no_domain_specific_prompt_in_generic_set() -> None:
    for task_type in ("summarization", "qa", "documentation", "migration"):
        assert not any("Java" in p for p in _sanity_prompts("x", task_type))
