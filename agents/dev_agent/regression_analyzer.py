"""
agents/dev_agent/regression_analyzer.py -- compares baseline vs candidate
performance across the nine metrics the AI Developer agent must protect:
Net Profit, Profit Factor, Win Rate, Drawdown, Sharpe Ratio, Sortino
Ratio, Recovery Factor, Expectancy, Trade Count.

backtest.compute_advanced_trade_stats() already produces net_pnl,
profit_factor, win_rate, max_drawdown, expectancy, sharpe_ratio, and
total_trades. enrich_stats() adds the two it doesn't -- Sortino Ratio and
Recovery Factor -- computed here from the same per-trade points list,
rather than modifying backtest.py itself: that file is a Priority-1
production module the agent framework must not need to touch to do its
own validation job.

Every threshold in agents.config defaults to 0.0 -- ANY regression on
ANY tracked metric rejects, per explicit instruction ("reject any change
that decreases profitability, increases drawdown, reduces stability, or
causes test failures"). This is intentionally stricter than a typical CI
regression gate that only flags.
"""
import statistics

from .. import config

# (stats key, config threshold attr, higher_is_better)
METRICS = (
    ("net_pnl", "MAX_NET_PNL_REGRESSION_PCT", True),
    ("profit_factor", "MAX_PROFIT_FACTOR_REGRESSION_PCT", True),
    ("win_rate", "MAX_WIN_RATE_REGRESSION_PCT", True),
    ("max_drawdown", "MAX_DRAWDOWN_INCREASE_PCT", False),
    ("sharpe_ratio", "MAX_SHARPE_RATIO_REGRESSION_PCT", True),
    ("sortino_ratio", "MAX_SORTINO_RATIO_REGRESSION_PCT", True),
    ("recovery_factor", "MAX_RECOVERY_FACTOR_REGRESSION_PCT", True),
    ("expectancy", "MAX_EXPECTANCY_REGRESSION_PCT", True),
    ("total_trades", "MAX_TRADE_COUNT_REGRESSION_PCT", True),
)


def enrich_stats(stats: dict, points: list) -> dict:
    """Adds sortino_ratio and recovery_factor to a
    compute_advanced_trade_stats()-shaped dict, given the same per-trade
    points list that produced it. None-safe, same philosophy as
    backtest.py's own stats function: an undefined ratio is None, not 0
    or infinity."""
    enriched = dict(stats)

    downside = [p for p in points if p < 0]
    sortino = None
    if len(points) >= 2 and downside:
        downside_dev = statistics.pstdev(downside) if len(downside) >= 2 else abs(downside[0])
        if downside_dev > 0:
            sortino = round(statistics.mean(points) / downside_dev, 3)
    enriched["sortino_ratio"] = sortino

    max_dd = stats.get("max_drawdown") or 0.0
    net_pnl = stats.get("net_pnl") or 0.0
    enriched["recovery_factor"] = round(net_pnl / max_dd, 3) if max_dd > 0 else None

    return enriched


def _pct_change(baseline, candidate):
    if baseline in (None, 0):
        return None
    return round((candidate - baseline) / abs(baseline) * 100, 2)


def _is_regression(baseline, candidate, threshold_pct, higher_is_better):
    if baseline is None or candidate is None:
        return False
    if higher_is_better:
        return candidate < baseline * (1 - threshold_pct / 100)
    if baseline > 0:
        return candidate > baseline * (1 + threshold_pct / 100)
    return candidate > 0 and threshold_pct <= 0


def compare(baseline: dict, candidate: dict) -> dict:
    """Returns {"metrics": [...], "regressions": [...]}: one entry per
    metric in METRICS, plus the subset that breached its threshold. A
    metric that's None in either run is reported but never flagged as a
    regression -- e.g. profit_factor is None whenever a run has zero
    losing trades, which is not itself a problem."""
    metrics = []
    regressions = []
    for key, threshold_attr, higher_is_better in METRICS:
        b = baseline.get(key)
        c = candidate.get(key)
        threshold = getattr(config, threshold_attr)
        is_regression = _is_regression(b, c, threshold, higher_is_better)
        entry = {
            "metric": key, "baseline": b, "candidate": c,
            "pct_change": _pct_change(b, c), "higher_is_better": higher_is_better,
            "threshold_pct": threshold, "regression": is_regression,
        }
        metrics.append(entry)
        if is_regression:
            regressions.append(entry)
    return {"metrics": metrics, "regressions": regressions}
