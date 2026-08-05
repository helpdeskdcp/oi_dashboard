"""
test_ichimoku_backtest.py -- regression tests for backtest.py's Ichimoku
wiring (simulate_ichimoku_trades, compute_ichimoku_accuracy_stats).
Synthetic candle archive via monkeypatch (load_intraday_candles) -- no real
parquet files touched, no live broker/session, same philosophy as
test_ichimoku_engine.py. Per project convention, never hits app.py's
/live-positions route or a real Angel One session.
"""
import datetime as dt

import numpy as np
import pandas as pd
import pytest

import backtest as bt

START = dt.datetime(2026, 1, 1, 9, 15)


def _synthetic_candles_df(n=600, slope=0.08, noise=0.3, seed=1):
    rng = np.random.default_rng(seed)
    base = 100 + np.cumsum(rng.normal(slope, noise, n))
    rows = []
    for i, o in enumerate(base):
        c = o + rng.normal(0, noise * 0.5)
        h = max(o, c) + abs(rng.normal(0, noise * 0.4))
        l = min(o, c) - abs(rng.normal(0, noise * 0.4))
        rows.append({"datetime": START + dt.timedelta(minutes=3 * i), "open": o, "high": h, "low": l,
                      "close": c, "volume": int(abs(rng.normal(1000, 300))) + 50})
    return pd.DataFrame(rows)


def test_simulate_ichimoku_trades_no_archive_returns_empty_gracefully(monkeypatch):
    monkeypatch.setattr(bt, "load_intraday_candles", lambda symbol, timeframe="3m": pd.DataFrame(columns=["datetime", "open", "high", "low", "close"]))
    trades, n, meta = bt.simulate_ichimoku_trades("NOSUCHSYMBOL", "2026-01-01", "2026-01-02")
    assert trades == []
    assert "error" in meta


def test_simulate_ichimoku_trades_insufficient_history_reports_error(monkeypatch):
    tiny = _synthetic_candles_df(n=30)
    monkeypatch.setattr(bt, "load_intraday_candles", lambda symbol, timeframe="3m": tiny)
    trades, n, meta = bt.simulate_ichimoku_trades("TEST", "2026-01-01", "2026-01-03")
    assert trades == []
    assert "error" in meta


def test_simulate_ichimoku_trades_produces_well_formed_trades_on_a_trend(monkeypatch):
    df = _synthetic_candles_df(n=600, slope=0.1, noise=0.25, seed=5)
    monkeypatch.setattr(bt, "load_intraday_candles", lambda symbol, timeframe="3m": df)
    date_from = df["datetime"].iloc[0].date().isoformat()
    date_to = df["datetime"].iloc[-1].date().isoformat()
    trades, n, meta = bt.simulate_ichimoku_trades("TEST", date_from, date_to)
    assert "error" not in meta
    assert n == len(df)
    for t in trades:
        assert t["exit_reason"] in ("TARGET HIT", "STOP LOSS", "TIME EXIT")
        assert t["direction"] in ("BUY", "SELL")
        assert t["exit_time"] > t["entry_time"]
        # target must be on the correct side of entry for the trade's direction
        if t["direction"] == "BUY":
            assert t["target_price"] > t["entry_price"] > t["sl_price"]
        else:
            assert t["target_price"] < t["entry_price"] < t["sl_price"]


def test_simulate_ichimoku_trades_never_has_two_open_positions_at_once(monkeypatch):
    df = _synthetic_candles_df(n=600, slope=0.0, noise=0.6, seed=7)   # choppy -- likely to flip signals often
    monkeypatch.setattr(bt, "load_intraday_candles", lambda symbol, timeframe="3m": df)
    date_from = df["datetime"].iloc[0].date().isoformat()
    date_to = df["datetime"].iloc[-1].date().isoformat()
    trades, n, meta = bt.simulate_ichimoku_trades("TEST", date_from, date_to)
    for prev, cur in zip(trades, trades[1:]):
        assert cur["entry_time"] >= prev["exit_time"]   # never overlapping


def test_simulate_ichimoku_trades_respects_date_range_filter(monkeypatch):
    df = _synthetic_candles_df(n=600, slope=0.08, seed=9)   # spans 2 distinct calendar dates (verified below)
    monkeypatch.setattr(bt, "load_intraday_candles", lambda symbol, timeframe="3m": df)
    all_dates = sorted(df["datetime"].dt.date.unique())
    assert len(all_dates) >= 2, "fixture must span multiple days for this filter test to mean anything"
    full_from, full_to = all_dates[0].isoformat(), all_dates[-1].isoformat()
    _, n_full, _ = bt.simulate_ichimoku_trades("TEST", full_from, full_to)
    _, n_partial, meta_partial = bt.simulate_ichimoku_trades("TEST", full_from, all_dates[0].isoformat())
    if "error" not in meta_partial:
        assert n_partial < n_full


# ----------------------------------------------------------------------------
# compute_ichimoku_accuracy_stats -- pure function, no monkeypatching needed
# ----------------------------------------------------------------------------

def _fake_trade(direction, points, exit_reason, entry_time, hold_minutes=15):
    entry = entry_time
    exit_ = entry_time + dt.timedelta(minutes=hold_minutes)
    return {"direction": direction, "points": points, "exit_reason": exit_reason,
            "entry_time": entry, "exit_time": exit_}


def test_accuracy_stats_empty_trades_is_none_safe():
    stats = bt.compute_ichimoku_accuracy_stats([])
    assert stats["total_trades"] == 0
    assert stats["false_buy_pct"] is None
    assert stats["avg_holding_minutes"] is None


def test_accuracy_stats_false_buy_and_sell_pct():
    t0 = START
    trades = [
        _fake_trade("BUY", 10, "TARGET HIT", t0),
        _fake_trade("BUY", -5, "STOP LOSS", t0 + dt.timedelta(hours=1)),
        _fake_trade("BUY", -5, "STOP LOSS", t0 + dt.timedelta(hours=2)),
        _fake_trade("SELL", 8, "TARGET HIT", t0 + dt.timedelta(hours=3)),
    ]
    stats = bt.compute_ichimoku_accuracy_stats(trades)
    # 2 of 3 BUY trades were stop-losses -> false_buy_pct = 66.7
    assert stats["false_buy_pct"] == pytest.approx(66.7, abs=0.1)
    # 0 of 1 SELL trades were stop-losses -> false_sell_pct = 0.0
    assert stats["false_sell_pct"] == 0.0
    assert stats["avg_holding_minutes"] == 15.0
    assert stats["total_trades"] == 4
    assert stats["win_rate"] == 50.0


def test_accuracy_stats_reuses_compute_advanced_trade_stats_fields():
    trades = [_fake_trade("BUY", 10, "TARGET HIT", START), _fake_trade("SELL", -5, "STOP LOSS", START + dt.timedelta(hours=1))]
    stats = bt.compute_ichimoku_accuracy_stats(trades)
    base = bt.compute_advanced_trade_stats(trades)
    for key in base:
        assert stats[key] == base[key]
