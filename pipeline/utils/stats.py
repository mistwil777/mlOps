"""Statistical utilities: confidence intervals, McNemar test, sample size guidance."""

from __future__ import annotations

import math
from typing import NamedTuple


class ConfidenceInterval(NamedTuple):
    point: float       # observed proportion (0–1)
    low: float         # lower bound
    high: float        # upper bound
    margin: float      # half-width = (high - low) / 2
    n: int             # sample size


class McNemarResult(NamedTuple):
    b: int             # model_a correct, model_b wrong
    c: int             # model_b correct, model_a wrong
    statistic: float   # chi-squared statistic (with continuity correction)
    p_value: float     # two-sided p-value
    significant: bool  # p < 0.05
    winner: str        # "a", "b", or "tie"


def wilson_ci(k: int, n: int, confidence: float = 0.95) -> ConfidenceInterval:
    """Wilson score interval for a proportion k/n at the given confidence level.

    Preferred over Wald (p ± z*sqrt(p(1-p)/n)) because it stays in [0,1]
    even for extreme proportions and small n.
    """
    if n == 0:
        return ConfidenceInterval(0.0, 0.0, 0.0, 0.0, 0)

    z = _z_score(confidence)
    p_hat = k / n
    z2 = z * z
    denom = 1 + z2 / n

    centre = (p_hat + z2 / (2 * n)) / denom
    half = (z / denom) * math.sqrt(p_hat * (1 - p_hat) / n + z2 / (4 * n * n))

    low = max(0.0, centre - half)
    high = min(1.0, centre + half)
    return ConfidenceInterval(
        point=round(p_hat, 4),
        low=round(low, 4),
        high=round(high, 4),
        margin=round((high - low) / 2, 4),
        n=n,
    )


def mcnemar_test(
    correct_a: list[bool],
    correct_b: list[bool],
) -> McNemarResult:
    """McNemar's test for paired binary outcomes (with Edwards continuity correction).

    correct_a[i] / correct_b[i] = True if model A / B was correct on example i.
    Only the discordant pairs matter: b (A right, B wrong) and c (B right, A wrong).
    """
    if len(correct_a) != len(correct_b):
        raise ValueError("correct_a and correct_b must have the same length")

    b = sum(1 for a, bb in zip(correct_a, correct_b) if a and not bb)
    c = sum(1 for a, bb in zip(correct_a, correct_b) if not a and bb)

    discordant = b + c
    if discordant == 0:
        return McNemarResult(b=b, c=c, statistic=0.0, p_value=1.0, significant=False, winner="tie")

    # Edwards continuity correction: (|b - c| - 1)^2 / (b + c)
    numerator = max(0.0, abs(b - c) - 1.0) ** 2
    stat = numerator / discordant
    p_value = _chi2_sf(stat, df=1)

    if p_value < 0.05:
        winner = "a" if b > c else "b"
    else:
        winner = "tie"

    return McNemarResult(
        b=b, c=c,
        statistic=round(stat, 4),
        p_value=round(p_value, 4),
        significant=p_value < 0.05,
        winner=winner,
    )


def min_samples_for_margin(target_margin: float = 0.05, confidence: float = 0.95) -> int:
    """Return the minimum n so that the Wilson CI half-width ≤ target_margin.

    Uses the conservative estimate p=0.5 (worst case, widest interval).
    target_margin=0.05 → ±5 pp is generally considered the threshold for
    meaningful comparison between models.
    """
    z = _z_score(confidence)
    # Wald approximation at p=0.5: n ≥ (z / (2 * margin))^2
    n = math.ceil((z / (2 * target_margin)) ** 2)
    return n


def sample_size_warning(n: int, target_margin: float = 0.05) -> dict:
    """Return a diagnostic dict describing whether n is sufficient.

    Returns:
        {
          "sufficient": bool,
          "current_margin": float,   # ±pp at p=0.5 with current n
          "recommended_n": int,      # n needed for target_margin
          "message": str,
        }
    """
    recommended = min_samples_for_margin(target_margin)
    # current margin at p=0.5 (worst case)
    ci = wilson_ci(n // 2, n) if n > 0 else ConfidenceInterval(0.5, 0.0, 1.0, 0.5, 0)
    current_margin = ci.margin

    if n >= recommended:
        return {
            "sufficient": True,
            "current_margin": round(current_margin, 3),
            "recommended_n": recommended,
            "message": f"Taille suffisante ({n} exemples, marge ±{current_margin*100:.1f} pp).",
        }

    return {
        "sufficient": False,
        "current_margin": round(current_margin, 3),
        "recommended_n": recommended,
        "message": (
            f"Taille insuffisante : {n} exemples → marge ±{current_margin*100:.1f} pp. "
            f"Ajoutez au moins {recommended - n} exemples pour descendre à ±{target_margin*100:.0f} pp "
            f"et rendre les comparaisons fiables."
        ),
    }


# ── Internal helpers ──────────────────────────────────────────────────────────

def _z_score(confidence: float) -> float:
    """Two-tailed z-score for a given confidence level (approximation via erfinv)."""
    # z = sqrt(2) * erfinv(confidence)
    return math.sqrt(2) * _erfinv(confidence)


def _erfinv(x: float) -> float:
    """Rational approximation to erfinv (Winitzki 2008), accurate to ~1e-4."""
    a = 0.147
    ln = math.log(1 - x * x)
    term = 2 / (math.pi * a) + ln / 2
    return math.copysign(
        math.sqrt(math.sqrt(term * term - ln / a) - term),
        x,
    )


def _chi2_sf(x: float, df: int = 1) -> float:
    """Survival function of chi-squared distribution (P(X > x)).

    For df=1: chi2_sf(x) = erfc(sqrt(x/2)) = 1 - erf(sqrt(x/2)).
    Uses math.erfc for accuracy.
    """
    if df != 1:
        raise NotImplementedError("Only df=1 supported")
    if x <= 0:
        return 1.0
    return math.erfc(math.sqrt(x / 2))
