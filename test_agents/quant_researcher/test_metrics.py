"""
test_agents/quant_researcher/test_metrics.py -- confirms metrics.py
correctly delegates to data_access.compute_advanced_trade_stats (never a
real import of backtest.py in this test) and adds Recovery Factor /
Trade Count on top.
"""
from agents.quant_researcher import metrics


def test_delegates_to_data_access_and_adds_recovery_factor(monkeypatch):
    fake_stats = {"net_pnl": 100.0, "max_drawdown": 25.0, "total_trades": 4, "profit_factor": 2.0}
    monkeypatch.setattr(metrics.data_access, "compute_advanced_trade_stats", lambda trades: dict(fake_stats))

    stats = metrics.compute_stats([{"points": 10}])

    assert stats["net_pnl"] == 100.0
    assert stats["profit_factor"] == 2.0
    assert stats["recovery_factor"] == 4.0  # 100 / 25
    assert stats["trade_count"] == 4


def test_recovery_factor_is_none_when_no_drawdown(monkeypatch):
    monkeypatch.setattr(
        metrics.data_access, "compute_advanced_trade_stats",
        lambda trades: {"net_pnl": 0.0, "max_drawdown": 0.0, "total_trades": 0},
    )
    stats = metrics.compute_stats([])
    assert stats["recovery_factor"] is None
