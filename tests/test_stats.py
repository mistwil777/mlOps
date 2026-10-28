"""Tests for pipeline/utils/stats.py — Wilson CI, McNemar, sample size."""

import math
import pytest
from pipeline.utils.stats import (
    wilson_ci,
    mcnemar_test,
    min_samples_for_margin,
    sample_size_warning,
)


class TestWilsonCI:
    def test_empty_sample(self):
        ci = wilson_ci(0, 0)
        assert ci.point == 0.0
        assert ci.low == 0.0
        assert ci.high == 0.0
        assert ci.n == 0

    def test_all_pass(self):
        ci = wilson_ci(50, 50)
        assert ci.point == 1.0
        assert ci.low > 0.9
        assert ci.high == 1.0

    def test_all_fail(self):
        ci = wilson_ci(0, 50)
        assert ci.point == 0.0
        assert ci.low == 0.0
        assert ci.high < 0.1

    def test_72pct_50_samples(self):
        ci = wilson_ci(36, 50)
        assert ci.point == 0.72
        # With n=50, margin should be large (> 10 pp)
        assert ci.margin > 0.10
        assert ci.low < 0.72
        assert ci.high > 0.72

    def test_bounds_always_valid(self):
        for k in [0, 10, 50, 100, 200]:
            for n in [1, 10, 50, 200, 500]:
                if k > n:
                    continue
                ci = wilson_ci(k, n)
                assert 0.0 <= ci.low <= ci.high <= 1.0, f"Invalid CI for k={k}, n={n}"

    def test_larger_n_gives_smaller_margin(self):
        ci_small = wilson_ci(36, 50)
        ci_large = wilson_ci(360, 500)
        assert ci_large.margin < ci_small.margin

    def test_interval_contains_point_estimate(self):
        ci = wilson_ci(25, 100)
        assert ci.low <= ci.point <= ci.high


class TestMcNemar:
    def test_identical_models_no_difference(self):
        correct = [True] * 40 + [False] * 10
        result = mcnemar_test(correct, correct)
        assert not result.significant
        assert result.winner == "tie"
        assert result.b == 0
        assert result.c == 0

    def test_model_a_clearly_better(self):
        # A correct on 20 examples where B failed; B correct on 0 where A failed
        n = 50
        correct_a = [True] * 40 + [False] * 10
        correct_b = [True] * 20 + [False] * 30
        result = mcnemar_test(correct_a, correct_b)
        assert result.significant
        assert result.winner == "a"
        assert result.b > result.c

    def test_model_b_clearly_better(self):
        correct_a = [True] * 20 + [False] * 30
        correct_b = [True] * 40 + [False] * 10
        result = mcnemar_test(correct_a, correct_b)
        assert result.significant
        assert result.winner == "b"

    def test_balanced_discordant_no_winner(self):
        # 5 cases A>B, 5 cases B>A — tie
        correct_a = [True, True, True, True, True, False, False, False, False, False]
        correct_b = [False, False, False, False, False, True, True, True, True, True]
        result = mcnemar_test(correct_a, correct_b)
        assert not result.significant
        assert result.winner == "tie"

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            mcnemar_test([True, False], [True])

    def test_p_value_range(self):
        result = mcnemar_test([True] * 30 + [False] * 20, [True] * 20 + [False] * 30)
        assert 0.0 <= result.p_value <= 1.0


class TestSampleSize:
    def test_min_samples_monotone_in_margin(self):
        # Tighter margin → more samples needed, regardless of the actual value
        for margin in [0.10, 0.08, 0.05, 0.03, 0.02]:
            prev = min_samples_for_margin(margin + 0.01)
            curr = min_samples_for_margin(margin)
            assert curr >= prev, f"n({margin}) should be >= n({margin + 0.01})"

    def test_min_samples_sufficient_for_margin(self):
        # The returned n should actually achieve the requested margin at p=0.5
        for margin in [0.10, 0.05, 0.03]:
            n = min_samples_for_margin(margin)
            ci = wilson_ci(n // 2, n)
            assert ci.margin <= margin + 0.005, (
                f"n={n} for margin={margin} gives actual margin {ci.margin:.4f}"
            )

    def test_warning_uses_same_threshold_as_min_samples(self):
        # sample_size_warning and min_samples_for_margin must agree
        target = 0.05
        expected_n = min_samples_for_margin(target)
        w = sample_size_warning(expected_n - 1, target_margin=target)
        assert not w["sufficient"]
        w2 = sample_size_warning(expected_n, target_margin=target)
        assert w2["sufficient"]

    def test_warning_message_contains_actual_n(self):
        w = sample_size_warning(50)
        assert "50" in w["message"]

    def test_warning_current_margin_reflects_n(self):
        w_small = sample_size_warning(50)
        w_large = sample_size_warning(500)
        assert w_small["current_margin"] > w_large["current_margin"]

    def test_warning_zero(self):
        w = sample_size_warning(0)
        assert not w["sufficient"]
