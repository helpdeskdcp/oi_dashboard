"""
test_agents/dev_agent/test_regression_analyzer.py -- regression tests for
agents/dev_agent/regression_analyzer.py: enrich_stats() (Sortino Ratio,
Recovery Factor derivation) and compare() (all nine tracked metrics
against agents.config's zero-tolerance thresholds).
"""
import pytest

from agents import config
from agents.dev_agent import regression_analyzer as ra


class TestEnrichStats:
    def test_computes_sortino_and_recovery_factor(self):
        stats = {"net_pnl": 100.0, "max_drawdown": 20.0}
        points = [50.0, -10.0, 60.0, -15.0]
        enriched = ra.enrich_stats(stats, points)
        assert enriched["sortino_ratio"] is not None
        assert enriched["recovery_factor"] == pytest.approx(100.0 / 20.0)

    def test_none_when_no_losing_trades(self):
        stats = {"net_pnl": 100.0, "max_drawdown": 0.0}
        points = [10.0, 20.0, 30.0]
        enriched = ra.enrich_stats(stats, points)
        assert enriched["sortino_ratio"] is None
        assert enriched["recovery_factor"] is None  # max_drawdown 0 -> undefined, not infinite

    def test_empty_trades_does_not_raise(self):
        stats = {"net_pnl": 0.0, "max_drawdown": 0.0}
        enriched = ra.enrich_stats(stats, [])
        assert enriched["sortino_ratio"] is None
        assert enriched["recovery_factor"] is None

    def test_preserves_original_stats_keys(self):
        stats = {"net_pnl": 5.0, "max_drawdown": 1.0, "win_rate": 55.0}
        enriched = ra.enrich_stats(stats, [5.0, -1.0])
        assert enriched["win_rate"] == 55.0
        assert enriched["net_pnl"] == 5.0


class TestCompare:
    def _stats(self, **overrides):
        base = {
            "net_pnl": 100.0, "profit_factor": 2.0, "win_rate": 55.0,
            "max_drawdown": 20.0, "sharpe_ratio": 0.5, "sortino_ratio": 0.8,
            "recovery_factor": 5.0, "expectancy": 10.0, "total_trades": 10,
        }
        base.update(overrides)
        return base

    def test_identical_stats_have_zero_regressions(self):
        baseline = self._stats()
        candidate = self._stats()
        result = ra.compare(baseline, candidate)
        assert result["regressions"] == []
        assert len(result["metrics"]) == len(ra.METRICS)

    def test_improved_candidate_has_zero_regressions(self):
        baseline = self._stats()
        candidate = self._stats(net_pnl=150.0, profit_factor=3.0, win_rate=60.0, max_drawdown=10.0)
        result = ra.compare(baseline, candidate)
        assert result["regressions"] == []

    @pytest.mark.parametrize("metric,threshold_attr,higher_is_better", ra.METRICS)
    def test_each_metric_flags_a_regression_at_zero_tolerance(self, metric, threshold_attr, higher_is_better):
        assert getattr(config, threshold_attr) == 0.0  # this test assumes the zero-tolerance default
        baseline = self._stats()
        worse_value = baseline[metric] * (0.9 if higher_is_better else 1.1)
        candidate = self._stats(**{metric: worse_value})
        result = ra.compare(baseline, candidate)
        flagged = {r["metric"] for r in result["regressions"]}
        assert metric in flagged

    def test_missing_metric_in_either_run_is_reported_but_not_flagged(self):
        baseline = self._stats(profit_factor=None)
        candidate = self._stats(profit_factor=None)
        result = ra.compare(baseline, candidate)
        pf_entry = next(m for m in result["metrics"] if m["metric"] == "profit_factor")
        assert pf_entry["regression"] is False

    def test_drawdown_increase_from_zero_baseline_is_a_regression(self):
        baseline = self._stats(max_drawdown=0.0)
        candidate = self._stats(max_drawdown=5.0)
        result = ra.compare(baseline, candidate)
        dd_entry = next(m for m in result["metrics"] if m["metric"] == "max_drawdown")
        assert dd_entry["regression"] is True

    def test_pct_change_is_computed_for_present_metrics(self):
        baseline = self._stats(net_pnl=100.0)
        candidate = self._stats(net_pnl=110.0)
        result = ra.compare(baseline, candidate)
        pnl_entry = next(m for m in result["metrics"] if m["metric"] == "net_pnl")
        assert pnl_entry["pct_change"] == pytest.approx(10.0)
