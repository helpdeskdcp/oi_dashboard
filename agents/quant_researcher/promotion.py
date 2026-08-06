"""
agents/quant_researcher/promotion.py -- "Only promote strategies if they
outperform the current production strategy based on objective metrics.
Otherwise archive them." One function, one objective rule, no subjective
judgment call: a candidate must never be worse than the production
baseline on any tracked metric, and must be strictly better on at least
one. A statistical-validation failure vetoes promotion unconditionally,
before the metric comparison is even consulted.
"""
import dataclasses

PRIMARY_METRICS_HIGHER_IS_BETTER = ("net_pnl", "profit_factor", "sharpe_ratio", "expectancy", "recovery_factor")
DRAWDOWN_METRIC = "max_drawdown"  # lower is better


@dataclasses.dataclass
class PromotionDecision:
    should_promote: bool
    reasoning: str
    comparison: dict


# Metrics that come back None specifically because the result was
# "too good to have a denominator", not because of missing/insufficient
# data: profit_factor (no losing trades -- gross_loss is 0), sharpe_ratio
# (given decide_promotion is only ever called after
# statistics_validation.validate() already required >=30 trades and
# mean > 0, a None sharpe here can only mean zero-variance -- every trade
# an identical, positive result), recovery_factor (net P&L / max
# drawdown -- None only when max_drawdown is 0, i.e. never once
# underwater). None of these should collapse to 0.0 the way genuinely
# missing data would -- a flawless track record must never lose a
# promotion comparison because its strength was undefined rather than
# merely large.
_UNDEFINED_MEANS_PERFECT = ("profit_factor", "sharpe_ratio", "recovery_factor")


def _safe(stats: dict, key: str) -> float:
    v = stats.get(key)
    if v is not None:
        return v
    if key in _UNDEFINED_MEANS_PERFECT and (stats.get("total_trades") or 0) > 0:
        return float("inf")
    return 0.0


def decide_promotion(candidate_stats: dict, baseline_stats: dict, *, validation_passed: bool) -> PromotionDecision:
    if not validation_passed:
        return PromotionDecision(
            should_promote=False,
            reasoning="candidate did not pass statistical validation -- never promoted on sample-size "
                      "or significance grounds alone, however the raw metrics look.",
            comparison={},
        )

    comparison = {}
    worse_on_any = False
    better_on_any = False

    for metric in PRIMARY_METRICS_HIGHER_IS_BETTER:
        c, b = _safe(candidate_stats, metric), _safe(baseline_stats, metric)
        comparison[metric] = {"candidate": c, "baseline": b, "delta": round(c - b, 4)}
        if c < b:
            worse_on_any = True
        elif c > b:
            better_on_any = True

    c_dd, b_dd = _safe(candidate_stats, DRAWDOWN_METRIC), _safe(baseline_stats, DRAWDOWN_METRIC)
    comparison[DRAWDOWN_METRIC] = {"candidate": c_dd, "baseline": b_dd, "delta": round(c_dd - b_dd, 4)}
    if c_dd > b_dd:
        worse_on_any = True
    elif c_dd < b_dd:
        better_on_any = True

    should_promote = better_on_any and not worse_on_any
    reasoning = (
        "candidate matches or beats the production baseline on every tracked metric, and "
        "strictly beats it on at least one -- promoting for five-gate approval."
        if should_promote else
        "candidate is worse than the production baseline on at least one tracked metric, or "
        "fails to beat it on any -- archived, not promoted."
    )
    return PromotionDecision(should_promote=should_promote, reasoning=reasoning, comparison=comparison)
