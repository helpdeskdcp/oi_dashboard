"""
Fast regression counterpart to scripts/hardening/market_replay.py -- a
short (3-day, single-symbol) real replay so the code path stays covered
by every `pytest` run without re-running the full 30-day, 8-symbol
sweep (which takes several minutes -- see PRODUCTION_HARDENING_SPRINT.md
for those real numbers).
"""
import datetime as dt

import backtest


def test_ichimoku_replay_runs_end_to_end_on_real_recent_candle_data():
    candles = backtest.load_intraday_candles("NIFTY")
    if candles.empty:
        import pytest
        pytest.skip("no NIFTY candle archive in this checkout")

    latest = candles["datetime"].max().date()
    date_from = latest - dt.timedelta(days=3)
    trades, candles_seen, meta = backtest.simulate_ichimoku_trades("NIFTY", date_from.isoformat(), latest.isoformat())

    assert "error" not in meta
    assert candles_seen > 0
    stats = backtest.compute_ichimoku_accuracy_stats(trades)
    assert stats["total_trades"] == len(trades)
    if trades:
        assert 0.0 <= stats["win_rate"] <= 100.0
