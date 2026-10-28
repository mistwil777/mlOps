"""Tests for pipeline/utils/schema.py — CanonicalPair, canonicalize, validate_pairs."""

import pytest
from pipeline.utils.schema import CanonicalPair, canonicalize, validate_pairs


class TestCanonicalPair:
    def test_required_fields_only(self):
        p = CanonicalPair(prompt="hello", completion="world")
        assert p.prompt == "hello"
        assert p.completion == "world"
        assert p.adapter_name is None
        assert p.lang_pair is None

    def test_to_dict_omits_none(self):
        p = CanonicalPair(prompt="p", completion="c")
        d = p.to_dict()
        assert "prompt" in d
        assert "completion" in d
        # None fields should be absent
        assert "adapter_name" not in d
        assert "lang_pair" not in d

    def test_to_dict_keeps_required_even_if_empty(self):
        p = CanonicalPair(prompt="", completion="")
        d = p.to_dict()
        assert "prompt" in d
        assert "completion" in d

    def test_from_dict_roundtrip(self):
        original = CanonicalPair(
            prompt="def f(): pass",
            completion="public void f() {}",
            adapter_name="java-to-python",
            lang_pair="python-java",
            domain="code",
        )
        d = original.to_dict()
        restored = CanonicalPair.from_dict(d)
        assert restored.prompt == original.prompt
        assert restored.completion == original.completion
        assert restored.adapter_name == original.adapter_name
        assert restored.lang_pair == original.lang_pair

    def test_from_dict_extra_keys_go_to_meta(self):
        d = {"prompt": "p", "completion": "c", "unknown_field": "value"}
        p = CanonicalPair.from_dict(d)
        assert p.meta.get("unknown_field") == "value"


class TestCanonicalize:
    def test_matched_transform_output(self):
        raw = {
            "prompt": "Migrate Java to Python",
            "completion": "x = 1",
            "source_url": "https://example.com",
            "adapter_name": "java-to-python",
            "meta": {"task_name": "hello", "src_lang": "java", "tgt_lang": "python", "tokens": 42},
            "collected_at": "2026-01-01T00:00:00+00:00",
        }
        p = canonicalize(raw, defaults={"lang_pair": "java-python", "domain": "code"})
        assert p.prompt == raw["prompt"]
        assert p.completion == raw["completion"]
        assert p.lang_pair == "java-python"
        assert p.domain == "code"
        assert p.meta.get("task_name") == "hello"
        # Legacy meta keys moved into meta
        assert p.meta.get("src_lang") == "java"

    def test_semantic_transform_output(self):
        raw = {
            "prompt": "Convert to Python",
            "completion": "x = 1",
            "source_lang": "java",
            "target_lang": "python",
            "similarity": 0.87,
            "source_url": "https://a.com",
            "target_url": "https://b.com",
            "source_desc": "java hello",
            "target_desc": "python hello",
            "meta": {},
        }
        p = canonicalize(raw)
        assert p.lang_pair == "java-python"
        assert p.lang == "java"
        assert p.similarity == 0.87
        assert p.source_url == "https://a.com"
        # target_url consumed, source_desc/target_desc dropped
        assert "target_url" not in p.to_dict()
        assert "source_desc" not in p.to_dict()

    def test_defaults_do_not_overwrite_explicit(self):
        raw = {"prompt": "p", "completion": "c", "lang_pair": "fr-en"}
        p = canonicalize(raw, defaults={"lang_pair": "java-python"})
        assert p.lang_pair == "fr-en"

    def test_none_defaults_ignored(self):
        raw = {"prompt": "p", "completion": "c"}
        p = canonicalize(raw, defaults={"domain": None, "lang_pair": None})
        assert p.domain is None
        assert p.lang_pair is None


class TestValidatePairs:
    def test_valid_pairs_no_issues(self):
        pairs = [
            CanonicalPair(prompt="p1", completion="c1", adapter_name="adapter-a"),
            CanonicalPair(prompt="p2", completion="c2", adapter_name="adapter-b"),
        ]
        assert validate_pairs(pairs) == []

    def test_empty_prompt_flagged(self):
        pairs = [CanonicalPair(prompt="", completion="c", adapter_name="a")]
        issues = validate_pairs(pairs)
        assert any(i["field"] == "prompt" for i in issues)

    def test_empty_completion_flagged(self):
        pairs = [CanonicalPair(prompt="p", completion="   ", adapter_name="a")]
        issues = validate_pairs(pairs)
        assert any(i["field"] == "completion" for i in issues)

    def test_missing_adapter_flagged_when_required(self):
        pairs = [CanonicalPair(prompt="p", completion="c")]
        issues = validate_pairs(pairs, require_adapter=True)
        assert any(i["field"] == "adapter_name" for i in issues)

    def test_missing_adapter_ok_when_not_required(self):
        pairs = [CanonicalPair(prompt="p", completion="c")]
        assert validate_pairs(pairs, require_adapter=False) == []

    def test_invalid_quality_score_flagged(self):
        pairs = [CanonicalPair(prompt="p", completion="c", quality_score=1.5)]
        issues = validate_pairs(pairs, require_adapter=False)
        assert any(i["field"] == "quality_score" for i in issues)

    def test_valid_quality_score_ok(self):
        pairs = [CanonicalPair(prompt="p", completion="c", quality_score=0.85)]
        assert validate_pairs(pairs, require_adapter=False) == []

    def test_issue_includes_index(self):
        pairs = [
            CanonicalPair(prompt="ok", completion="ok", adapter_name="a"),
            CanonicalPair(prompt="", completion="ok", adapter_name="a"),
        ]
        issues = validate_pairs(pairs)
        assert issues[0]["index"] == 1
