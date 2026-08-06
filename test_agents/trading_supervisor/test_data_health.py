"""
test_agents/trading_supervisor/test_data_health.py -- regression tests
for data_health.py. backtest.load_cycles is monkeypatched via a fake
module in sys.modules -- never a real import of backtest.py.
"""
import sys
import types
import datetime as dt

import pytest

from agents.trading_supervisor import data_health


@pytest.fixture()
def fake_backtest(monkeypatch):
    fake = types.ModuleType("backtest")
    monkeypatch.setitem(sys.modules, "backtest", fake)
    return fake


class TestLatestCycleTs:
    def test_returns_the_last_cycles_ts(self, fake_backtest):
        fake_backtest.load_cycles = lambda symbol, d1, d2: [
            {"cycle": {"ts": "2026-05-04T09:15:00"}}, {"cycle": {"ts": "2026-05-04T09:18:00"}},
        ]
        assert data_health.latest_cycle_ts("NIFTY") == "2026-05-04T09:18:00"

    def test_none_when_no_cycles(self, fake_backtest):
        fake_backtest.load_cycles = lambda symbol, d1, d2: []
        assert data_health.latest_cycle_ts("NIFTY") is None


class TestCheckFeedStaleness:
    def test_fresh_data_is_not_stale(self, fake_backtest):
        now = dt.datetime(2026, 5, 4, 9, 20, 0)
        fake_backtest.load_cycles = lambda symbol, d1, d2: [{"cycle": {"ts": "2026-05-04T09:18:00"}}]
        result = data_health.check_feed_staleness("NIFTY", staleness_minutes=15, now=now)
        assert result.is_stale is False
        assert result.staleness_minutes == 2.0

    def test_old_data_is_stale(self, fake_backtest):
        now = dt.datetime(2026, 5, 4, 10, 0, 0)
        fake_backtest.load_cycles = lambda symbol, d1, d2: [{"cycle": {"ts": "2026-05-04T09:18:00"}}]
        result = data_health.check_feed_staleness("NIFTY", staleness_minutes=15, now=now)
        assert result.is_stale is True

    def test_no_cycles_at_all_is_stale(self, fake_backtest):
        fake_backtest.load_cycles = lambda symbol, d1, d2: []
        result = data_health.check_feed_staleness("NIFTY")
        assert result.is_stale is True
        assert result.latest_cycle_ts is None

    def test_a_data_access_failure_is_reported_as_stale_not_raised(self, fake_backtest):
        def boom(symbol, d1, d2):
            raise RuntimeError("no such table: cycles")

        fake_backtest.load_cycles = boom
        result = data_health.check_feed_staleness("NIFTY")
        assert result.is_stale is True
        assert "could not read" in result.note


class TestFailureClustering:
    def test_high_failure_rate_is_clustered(self):
        rows = [{"outcome": "rejected"}] * 8 + [{"outcome": "pending_approval"}] * 2
        result = data_health.failure_clustering(rows, window=10, threshold=0.6)
        assert result["clustered"] is True
        assert result["failure_rate"] == 0.8

    def test_low_failure_rate_is_not_clustered(self):
        rows = [{"outcome": "pending_approval"}] * 10
        result = data_health.failure_clustering(rows, window=10, threshold=0.6)
        assert result["clustered"] is False

    def test_empty_rows_returns_a_safe_default(self):
        result = data_health.failure_clustering([])
        assert result == {"clustered": False, "failure_rate": 0.0, "sample_size": 0}
