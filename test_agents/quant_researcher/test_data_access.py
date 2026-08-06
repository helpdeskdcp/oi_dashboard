"""
test_agents/quant_researcher/test_data_access.py -- confirms
data_access.py delegates to the real `backtest` module correctly,
without ever importing the real backtest.py (a fake module is injected
into sys.modules for the duration of each test) -- these tests would
still catch a signature drift between data_access.py and backtest.py's
actual functions if one of them changed, without needing a real
oi_history.db or data/history/ archive.
"""
import sys
import types

import pytest

from agents.quant_researcher import data_access


@pytest.fixture()
def fake_backtest(monkeypatch):
    fake = types.ModuleType("backtest")
    monkeypatch.setitem(sys.modules, "backtest", fake)
    return fake


def test_load_candles_delegates(fake_backtest):
    calls = {}

    def fake_load(symbol, timeframe="3m"):
        calls["args"] = (symbol, timeframe)
        return "the-dataframe"

    fake_backtest.load_intraday_candles = fake_load
    result = data_access.load_candles("NIFTY", timeframe="3m")
    assert result == "the-dataframe"
    assert calls["args"] == ("NIFTY", "3m")


def test_load_cycles_for_range_normalizes(fake_backtest):
    fake_backtest.load_cycles = lambda symbol, date_from, date_to: [
        {"cycle": {"id": 1, "ts": "2026-05-04T09:15:00", "atm": 100}, "rows": [{"strike": 100, "ce_oi": 5}]},
    ]
    result = data_access.load_cycles_for_range("NIFTY", "2026-05-04", "2026-05-04")
    assert result == [{"id": 1, "ts": "2026-05-04T09:15:00", "atm": 100, "strikes": [{"strike": 100, "ce_oi": 5}]}]


def test_normalize_cycles_handles_empty_input():
    assert data_access.normalize_cycles([]) == []
    assert data_access.normalize_cycles(None) == []


def test_compute_advanced_trade_stats_delegates(fake_backtest):
    fake_backtest.compute_advanced_trade_stats = lambda trades: {"total_trades": len(trades)}
    assert data_access.compute_advanced_trade_stats([{"points": 1}, {"points": 2}]) == {"total_trades": 2}


def test_production_baseline_stats_delegates(fake_backtest):
    fake_backtest.simulate_dynamic_sr_v4_trades = lambda symbol, d1, d2: ([{"points": 5}], 10, {})
    fake_backtest.compute_advanced_trade_stats = lambda trades: {"total_trades": len(trades), "net_pnl": 5}
    result = data_access.production_baseline_stats("NIFTY", "2026-05-01", "2026-05-04")
    assert result == {"total_trades": 1, "net_pnl": 5}
