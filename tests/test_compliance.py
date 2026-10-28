"""Tests for pipeline/utils/compliance.py — compliance report generation."""

import pytest
from pipeline.utils.compliance import (
    generate_compliance_report,
    ComplianceStatus,
    ComplianceReport,
)

_BASE_PROJECT = {
    "id": "test-project",
    "name": "Test Project",
    "domain": "code",
    "classification": "interne",
    "description": "A sufficiently long description for testing purposes here.",
    "quality_thresholds": {"min_pass_rate": 0.85},
    "data": {"raw": "datalake/projects/test/raw/"},
}

_BASE_EVAL = {
    "n": 100,
    "pass_rate": 0.90,
    "rouge_l": 0.70,
    "run_id": "run_abc123",
}

_BASE_MODEL = {
    "model_id": "model-v1",
    "model_name": "qwen2.5-coder-7b",
    "serving": "ollama",
    "bundle_type": "docker",
    "docker_bundle": True,
}


class TestComplianceReportStructure:
    def test_returns_report_object(self):
        report = generate_compliance_report(_BASE_PROJECT, _BASE_EVAL, _BASE_MODEL)
        assert isinstance(report, ComplianceReport)

    def test_has_all_frameworks(self):
        report = generate_compliance_report(_BASE_PROJECT, _BASE_EVAL, _BASE_MODEL)
        frameworks = {c.framework for c in report.checks}
        assert "NIS2" in frameworks
        assert "AI_ACT" in frameworks
        assert "OIV" in frameworks

    def test_to_dict_serializable(self):
        report = generate_compliance_report(_BASE_PROJECT, _BASE_EVAL, _BASE_MODEL)
        d = report.to_dict()
        assert "checks" in d
        assert "passed" in d
        assert "failed" in d
        assert "delivery_blocked" in d
        assert isinstance(d["checks"], list)

    def test_summary_contains_project_id(self):
        report = generate_compliance_report(_BASE_PROJECT, _BASE_EVAL, _BASE_MODEL)
        assert "test-project" in report.summary()


class TestNIS2Checks:
    def test_local_serving_passes(self):
        report = generate_compliance_report(_BASE_PROJECT, _BASE_EVAL, _BASE_MODEL)
        nis2_1 = next(c for c in report.checks if c.id == "NIS2-1")
        assert nis2_1.status == ComplianceStatus.PASS

    def test_cloud_serving_fails(self):
        model = {**_BASE_MODEL, "serving": "bedrock"}
        report = generate_compliance_report(_BASE_PROJECT, _BASE_EVAL, model)
        nis2_1 = next(c for c in report.checks if c.id == "NIS2-1")
        assert nis2_1.status == ComplianceStatus.FAIL
        assert report.delivery_blocked

    def test_with_eval_and_run_id_passes(self):
        report = generate_compliance_report(_BASE_PROJECT, _BASE_EVAL, _BASE_MODEL)
        nis2_2 = next(c for c in report.checks if c.id == "NIS2-2")
        assert nis2_2.status == ComplianceStatus.PASS

    def test_no_eval_fails(self):
        report = generate_compliance_report(_BASE_PROJECT, {}, _BASE_MODEL)
        nis2_2 = next(c for c in report.checks if c.id == "NIS2-2")
        assert nis2_2.status == ComplianceStatus.FAIL


class TestAIActChecks:
    def test_code_domain_not_high_risk(self):
        report = generate_compliance_report(_BASE_PROJECT, _BASE_EVAL, _BASE_MODEL)
        aiact_1 = next(c for c in report.checks if c.id == "AIACT-1")
        assert aiact_1.status == ComplianceStatus.PASS

    def test_defense_domain_is_high_risk_warning(self):
        project = {**_BASE_PROJECT, "domain": "defense", "classification": "secret"}
        report = generate_compliance_report(project, _BASE_EVAL, _BASE_MODEL)
        aiact_1 = next(c for c in report.checks if c.id == "AIACT-1")
        assert aiact_1.status == ComplianceStatus.WARNING

    def test_pass_rate_above_threshold_passes(self):
        report = generate_compliance_report(_BASE_PROJECT, _BASE_EVAL, _BASE_MODEL)
        aiact_2 = next(c for c in report.checks if c.id == "AIACT-2")
        assert aiact_2.status == ComplianceStatus.PASS

    def test_pass_rate_below_threshold_fails(self):
        eval_low = {**_BASE_EVAL, "pass_rate": 0.72}
        report = generate_compliance_report(_BASE_PROJECT, eval_low, _BASE_MODEL)
        aiact_2 = next(c for c in report.checks if c.id == "AIACT-2")
        assert aiact_2.status == ComplianceStatus.FAIL
        assert report.delivery_blocked

    def test_no_pass_rate_gives_warning(self):
        eval_no_rate = {k: v for k, v in _BASE_EVAL.items() if k != "pass_rate"}
        report = generate_compliance_report(_BASE_PROJECT, eval_no_rate, _BASE_MODEL)
        aiact_2 = next(c for c in report.checks if c.id == "AIACT-2")
        assert aiact_2.status == ComplianceStatus.WARNING

    def test_full_description_passes_transparency(self):
        report = generate_compliance_report(_BASE_PROJECT, _BASE_EVAL, _BASE_MODEL)
        aiact_3 = next(c for c in report.checks if c.id == "AIACT-3")
        assert aiact_3.status == ComplianceStatus.PASS


class TestOIVChecks:
    def test_non_oiv_project_gives_na(self):
        report = generate_compliance_report(_BASE_PROJECT, _BASE_EVAL, _BASE_MODEL)
        oiv_1 = next(c for c in report.checks if c.id == "OIV-1")
        oiv_2 = next(c for c in report.checks if c.id == "OIV-2")
        assert oiv_1.status == ComplianceStatus.NA
        assert oiv_2.status == ComplianceStatus.NA

    def test_defense_project_with_docker_passes_air_gap(self):
        project = {**_BASE_PROJECT, "domain": "defense", "classification": "secret"}
        report = generate_compliance_report(project, _BASE_EVAL, _BASE_MODEL)
        oiv_1 = next(c for c in report.checks if c.id == "OIV-1")
        assert oiv_1.status == ComplianceStatus.PASS

    def test_defense_project_without_docker_warns(self):
        project = {**_BASE_PROJECT, "domain": "defense", "classification": "secret"}
        model = {**_BASE_MODEL, "bundle_type": "tar", "docker_bundle": False}
        report = generate_compliance_report(project, _BASE_EVAL, model)
        oiv_1 = next(c for c in report.checks if c.id == "OIV-1")
        assert oiv_1.status == ComplianceStatus.WARNING

    def test_defense_with_local_data_passes_sovereignty(self):
        project = {**_BASE_PROJECT, "domain": "defense", "classification": "secret"}
        report = generate_compliance_report(project, _BASE_EVAL, _BASE_MODEL)
        oiv_2 = next(c for c in report.checks if c.id == "OIV-2")
        assert oiv_2.status == ComplianceStatus.PASS


class TestDeliveryBlocking:
    def test_all_good_not_blocked(self):
        report = generate_compliance_report(_BASE_PROJECT, _BASE_EVAL, _BASE_MODEL)
        assert not report.delivery_blocked

    def test_cloud_serving_blocks_delivery(self):
        model = {**_BASE_MODEL, "serving": "openai"}
        report = generate_compliance_report(_BASE_PROJECT, _BASE_EVAL, model)
        assert report.delivery_blocked

    def test_low_pass_rate_blocks_delivery(self):
        eval_bad = {**_BASE_EVAL, "pass_rate": 0.50}
        report = generate_compliance_report(_BASE_PROJECT, eval_bad, _BASE_MODEL)
        assert report.delivery_blocked

    def test_counts_are_consistent(self):
        report = generate_compliance_report(_BASE_PROJECT, _BASE_EVAL, _BASE_MODEL)
        total_applicable = sum(1 for c in report.checks if c.status != ComplianceStatus.NA)
        assert report.passed + report.failed + report.warnings == total_applicable
