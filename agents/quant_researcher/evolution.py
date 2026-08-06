"""
agents/quant_researcher/evolution.py -- "Improve existing strategies by:
Parameter optimisation, Feature selection, Combining strategies, Removing
weak rules. Store every evolution inside Strategy Evolution Memory."
Every function here takes a StrategySpec (data) and returns a new
StrategySpec (data) plus its measured stats -- nothing here writes
strategy code; codegen.py does that, and only for whatever a research
cycle decides is worth promoting.
"""
import dataclasses
import itertools

from .. import config
from . import metrics, strategy_runner


def _score(stats: dict) -> tuple:
    """Composite ranking score for grid search / feature selection --
    Sharpe Ratio first (risk-adjusted), Profit Factor as a tiebreaker.
    None-safe: a spec with no losing trades yet (profit_factor None) or
    too few trades for a Sharpe ratio ranks last, not crashes the sort."""
    sharpe = stats.get("sharpe_ratio")
    pf = stats.get("profit_factor")
    return (sharpe if sharpe is not None else float("-inf"),
            pf if pf is not None else float("-inf"))


def optimize_parameters(spec, candles, cycles=None, *, threshold_grid: dict | None = None,
                         target_stop_grid: list | None = None, expiry_dates=None) -> tuple:
    """Grid search over spec.thresholds (per-feature) and optionally
    (target_points, stop_points) pairs. Capped at
    config.QUANT_RESEARCH_MAX_GRID_COMBINATIONS combinations -- a
    research cycle stays a research cycle, not an unbounded sweep.
    Returns (best_spec, best_stats, all_results), all_results being
    [(spec, stats), ...] for every combination tried, best first."""
    threshold_grid = threshold_grid or {name: [spec.thresholds.get(name, 0.0)] for name in spec.features}
    target_stop_grid = target_stop_grid or [(spec.target_points, spec.stop_points)]

    feature_names = list(threshold_grid.keys())
    grids = [threshold_grid[f] for f in feature_names] if feature_names else [[None]]
    combos = list(itertools.product(*grids))
    trials = list(itertools.product(combos, target_stop_grid))[: config.QUANT_RESEARCH_MAX_GRID_COMBINATIONS]

    results = []
    for threshold_combo, (target_points, stop_points) in trials:
        new_thresholds = dict(spec.thresholds)
        if feature_names:
            new_thresholds.update(dict(zip(feature_names, threshold_combo)))
        candidate = dataclasses.replace(
            spec, thresholds=new_thresholds, target_points=target_points, stop_points=stop_points,
        )
        trades = strategy_runner.run_strategy(candidate, candles, cycles, expiry_dates=expiry_dates)
        stats = metrics.compute_stats(trades)
        results.append((candidate, stats))

    results.sort(key=lambda pair: _score(pair[1]), reverse=True)
    if not results:
        return spec, metrics.compute_stats([]), []
    best_spec, best_stats = results[0]
    return best_spec, best_stats, results


def select_features(spec, candles, cycles=None, *, expiry_dates=None) -> tuple:
    """"Removing weak rules": for a multi-feature spec, try dropping each
    feature one at a time and keep whichever subset scores best
    (including the original, full-feature spec) -- a feature that's
    actively hurting the composite score gets removed. Returns
    (best_spec, best_stats, all_results)."""
    trades = strategy_runner.run_strategy(spec, candles, cycles, expiry_dates=expiry_dates)
    if len(spec.features) < 2:
        return spec, metrics.compute_stats(trades), [(spec, metrics.compute_stats(trades))]

    candidates = [spec]
    for dropped in spec.features:
        remaining = [f for f in spec.features if f != dropped]
        candidates.append(dataclasses.replace(spec, features=remaining, name=f"{spec.name}-{dropped}"))

    results = []
    for candidate in candidates:
        candidate_trades = strategy_runner.run_strategy(candidate, candles, cycles, expiry_dates=expiry_dates)
        results.append((candidate, metrics.compute_stats(candidate_trades)))

    results.sort(key=lambda pair: _score(pair[1]), reverse=True)
    best_spec, best_stats = results[0]
    return best_spec, best_stats, results


def record_evolution_step(memory_store, spec, stats, *, change_summary: str,
                           rationale: str | None = None, audit_log_id: int | None = None) -> int:
    return memory_store.record_strategy_evolution(
        strategy_name=spec.hypothesis_id, version_label=spec.name,
        change_summary=change_summary, rationale=rationale or str(stats), audit_log_id=audit_log_id,
    )
