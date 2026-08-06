"""
agents/quant_researcher/statistics_validation.py -- "Reject strategies
that fail statistical confidence. Never promote a strategy because of a
small sample size." Two independent gates, both must pass:
  1. minimum trade count (agents.config.QUANT_RESEARCH_MIN_TRADES).
  2. the per-trade points distribution's mean is significantly > 0 at
     agents.config.QUANT_RESEARCH_CONFIDENCE_LEVEL, via a one-sided
     z-test on the sample mean. Valid once the trade count clears gate 1
     above (the Central Limit Theorem is doing the work, not an assumed
     normal population) -- deliberately not scipy-based: this repo has no
     scipy dependency in requirements.txt, and a z-test needs nothing
     scipy would provide beyond math.erf, already in the stdlib.
"""
import dataclasses
import math

from .. import config


@dataclasses.dataclass
class ValidationResult:
    passed: bool
    reason: str
    trade_count: int
    mean_points: float | None
    z_score: float | None
    p_value: float | None


def _one_sided_p_value(z: float) -> float:
    """P(Z >= z) for the standard normal distribution, via math.erf."""
    return 0.5 * (1 - math.erf(z / math.sqrt(2)))


def validate(trades: list, *, min_trades: int | None = None,
             confidence_level: float | None = None) -> ValidationResult:
    min_trades = config.QUANT_RESEARCH_MIN_TRADES if min_trades is None else min_trades
    confidence_level = config.QUANT_RESEARCH_CONFIDENCE_LEVEL if confidence_level is None else confidence_level
    n = len(trades)

    if n < min_trades:
        return ValidationResult(
            passed=False,
            reason=f"only {n} trade(s), fewer than the required minimum of {min_trades} -- "
                   f"never promoted on sample size this small, however good the numbers look.",
            trade_count=n, mean_points=None, z_score=None, p_value=None,
        )

    points = [t.get("points") or 0.0 for t in trades]
    mean = sum(points) / n
    variance = sum((p - mean) ** 2 for p in points) / (n - 1) if n > 1 else 0.0
    stdev = math.sqrt(variance)

    if stdev == 0:
        z = float("inf") if mean > 0 else (float("-inf") if mean < 0 else 0.0)
    else:
        z = mean / (stdev / math.sqrt(n))

    if math.isinf(z):
        p_value = 0.0 if z > 0 else 1.0
    elif z == 0.0:
        p_value = 0.5
    else:
        p_value = _one_sided_p_value(z)

    alpha = 1 - confidence_level
    passed = mean > 0 and p_value < alpha
    reason = (
        f"mean per-trade P&L {mean:.2f} over {n} trades is significant at p={p_value:.4f} "
        f"(< alpha={alpha:.4f})"
        if passed else
        f"mean per-trade P&L {mean:.2f} over {n} trades is NOT statistically significant "
        f"(p={p_value:.4f}, needs < alpha={alpha:.4f})"
    )
    return ValidationResult(
        passed=passed, reason=reason, trade_count=n, mean_points=round(mean, 3),
        z_score=(round(z, 3) if math.isfinite(z) else z), p_value=round(p_value, 4),
    )
