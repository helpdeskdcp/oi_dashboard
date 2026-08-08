"""
test_agents/quant_researcher/test_metrics.py -- confirms metrics.py
correctly delegates to data_access.compute_advanced_trade_stats (never a
real import of backtest.py in this test) and adds Recovery Factor /
Trade Count / Sortino Ratio / Equity Curve (Milestone 11, Module 11.6) on
top.
"""
from agents.quant_researcher import metrics


def _stub(monkeypatch, fake_stats):
    monkeypatch.setattr(metrics.data_access, "compute_advanced_trade_stats", lambda trades: dict(fake_stats))


def test_delegates_to_data_access_and_adds_recovery_factor(monkeypatch):
    fake_stats = {"net_pnl": 100.0, "max_drawdown": 25.0, "total_trades": 4, "profit_factor": 2.0}
    _stub(monkeypatch, fake_stats)

    stats = metrics.compute_stats([{"points": 10}])

    assert stats["net_pnl"] == 100.0
    assert stats["profit_factor"] == 2.0
    assert stats["recovery_factor"] == 4.0  # 100 / 25
    assert stats["trade_count"] == 4


def test_recovery_factor_is_none_when_no_drawdown(monkeypatch):
    _stub(monkeypatch, {"net_pnl": 0.0, "max_drawdown": 0.0, "total_trades": 0})
    stats = metrics.compute_stats([])
    assert stats["recovery_factor"] is None


def test_pre_existing_fields_are_untouched_by_this_module(monkeypatch):
    """Milestone 11, Module 11.6 must be byte-identical for every field
    that existed before it -- only new keys are added, nothing already
    there is recomputed or altered."""
    fake_stats = {
        "net_pnl": 100.0, "max_drawdown": 25.0, "total_trades": 4, "profit_factor": 2.0,
        "win_rate": 75.0, "sharpe_ratio": 1.234, "expectancy": 25.0,
    }
    _stub(monkeypatch, fake_stats)
    stats = metrics.compute_stats([{"points": 10}])
    for key, value in fake_stats.items():
        assert stats[key] == value


class TestSortinoRatio:
    """Plan's own Module 11.6 success criterion: match a hand-computed
    reference value on a fixed synthetic trade sequence -- the same
    discipline metrics.py's existing recovery-factor tests already use,
    applied here against a value worked out by hand, not just re-derived
    from the same formula the implementation itself uses."""

    def test_matches_a_hand_computed_reference_value(self, monkeypatch):
        # points = [10, -5, 20, -15, 5]; mean = 3.0
        # downside values (min(p, 0) per point): 0, -5, 0, -15, 0
        # mean of squares = (0 + 25 + 0 + 225 + 0) / 5 = 50
        # downside deviation = sqrt(50) = 7.0710678...
        # sortino = 3.0 / 7.0710678... = 0.42426... -> 0.424
        _stub(monkeypatch, {"net_pnl": 15.0, "max_drawdown": 15.0, "total_trades": 5})
        trades = [{"points": p} for p in (10.0, -5.0, 20.0, -15.0, 5.0)]
        stats = metrics.compute_stats(trades)
        assert stats["sortino_ratio"] == 0.424

    def test_none_with_fewer_than_two_trades(self, monkeypatch):
        _stub(monkeypatch, {"net_pnl": 10.0, "max_drawdown": 0.0, "total_trades": 1})
        stats = metrics.compute_stats([{"points": 10.0}])
        assert stats["sortino_ratio"] is None

    def test_none_when_there_is_no_downside_at_all(self, monkeypatch):
        """Every trade a win -- no downside variance to divide by, the
        same honest-degradation gate Sharpe Ratio uses for a zero stdev."""
        _stub(monkeypatch, {"net_pnl": 30.0, "max_drawdown": 0.0, "total_trades": 3})
        trades = [{"points": p} for p in (10.0, 10.0, 10.0)]
        stats = metrics.compute_stats(trades)
        assert stats["sortino_ratio"] is None

    def test_missing_points_field_treated_as_zero_never_raises(self, monkeypatch):
        _stub(monkeypatch, {"net_pnl": -5.0, "max_drawdown": 5.0, "total_trades": 3})
        # middle trade has no "points" key at all (e.g. a TIME EXIT never assigned one)
        trades = [{"points": 10.0}, {"exit_reason": "TIME EXIT"}, {"points": -15.0}]
        stats = metrics.compute_stats(trades)  # must not raise KeyError/TypeError
        assert stats["sortino_ratio"] is not None

    def test_empty_trades_is_none_not_a_crash(self, monkeypatch):
        _stub(monkeypatch, {"net_pnl": 0.0, "max_drawdown": 0.0, "total_trades": 0})
        stats = metrics.compute_stats([])
        assert stats["sortino_ratio"] is None


class TestEquityCurve:
    def test_matches_a_hand_computed_reference_sequence(self, monkeypatch):
        # points = [10, -5, 20, -15, 5] -> cumulative: 10, 5, 25, 10, 15
        _stub(monkeypatch, {"net_pnl": 15.0, "max_drawdown": 15.0, "total_trades": 5})
        trades = [{"points": p} for p in (10.0, -5.0, 20.0, -15.0, 5.0)]
        stats = metrics.compute_stats(trades)
        assert stats["equity_curve"] == [10.0, 5.0, 25.0, 10.0, 15.0]

    def test_empty_trades_is_an_empty_curve(self, monkeypatch):
        _stub(monkeypatch, {"net_pnl": 0.0, "max_drawdown": 0.0, "total_trades": 0})
        stats = metrics.compute_stats([])
        assert stats["equity_curve"] == []

    def test_one_point_per_trade_in_order(self, monkeypatch):
        _stub(monkeypatch, {"net_pnl": 30.0, "max_drawdown": 0.0, "total_trades": 3})
        trades = [{"points": p} for p in (10.0, 10.0, 10.0)]
        stats = metrics.compute_stats(trades)
        assert len(stats["equity_curve"]) == 3
        assert stats["equity_curve"][-1] == stats["net_pnl"]  # final point equals the total net P&L
