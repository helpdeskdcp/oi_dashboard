"""
test_agents/quant_researcher/test_strategy_runner.py -- regression tests
for the generic StrategySpec interpreter.
"""
import pandas as pd
import pytest

from agents.quant_researcher.strategy_runner import run_strategy
from agents.quant_researcher.strategy_spec import StrategySpec


def _spec(**overrides):
    base = dict(
        name="test", symbol="NIFTY", hypothesis_id="oi_delta_combo", features=["atr"],
        thresholds={"atr": -1.0},  # atr always >= 0, so threshold -1 always fires long
        direction="both", target_points=5.0, stop_points=5.0, max_hold_bars=5, params={},
    )
    base.update(overrides)
    return StrategySpec(**base)


def _candles(prices, *, start="2026-05-04 09:15:00"):
    rows = []
    ts = pd.Timestamp(start)
    for i, p in enumerate(prices):
        rows.append({
            "datetime": ts + pd.Timedelta(minutes=3 * i),
            "open": p, "high": p + 1, "low": p - 1, "close": p, "volume": 100,
        })
    return pd.DataFrame(rows)


class TestEmptyOrShortInput:
    def test_empty_candles_returns_no_trades(self):
        assert run_strategy(_spec(), pd.DataFrame(columns=["datetime", "open", "high", "low", "close"])) == []

    def test_single_row_returns_no_trades(self):
        assert run_strategy(_spec(), _candles([100.0])) == []


class TestEntryAndExit:
    def test_target_hit_produces_a_win(self):
        # Flat prices, then a jump that clears the +5 target on the entry bar.
        prices = [100.0] * 5 + [110.0] * 5
        candles = _candles(prices)
        trades = run_strategy(_spec(target_points=5.0, stop_points=50.0, max_hold_bars=10), candles)
        assert len(trades) == 1
        assert trades[0]["exit_reason"] == "TARGET HIT"
        assert trades[0]["points"] > 0
        assert trades[0]["direction"] == "long"

    def test_stop_hit_produces_a_loss(self):
        prices = [100.0] * 5 + [90.0] * 5
        candles = _candles(prices)
        trades = run_strategy(_spec(target_points=50.0, stop_points=5.0, max_hold_bars=10), candles)
        assert len(trades) == 1
        assert trades[0]["exit_reason"] == "STOP LOSS"
        assert trades[0]["points"] < 0

    def test_time_exit_when_neither_level_is_hit(self):
        prices = [100.0] * 5  # entry at bar 1, max_hold_bars=3 -> last bar is index 4 == n-1
        candles = _candles(prices)
        trades = run_strategy(_spec(target_points=50.0, stop_points=50.0, max_hold_bars=3), candles)
        assert len(trades) == 1
        assert trades[0]["exit_reason"] == "TIME EXIT"

    def test_no_overlapping_trades(self):
        prices = [100.0] * 30
        candles = _candles(prices)
        trades = run_strategy(_spec(target_points=50.0, stop_points=50.0, max_hold_bars=3), candles)
        for a, b in zip(trades, trades[1:]):
            assert a["exit_time"] <= b["entry_time"]

    def test_direction_long_only_never_shorts(self):
        prices = [100.0] * 5 + [90.0] * 5
        candles = _candles(prices)
        spec = _spec(direction="long", features=["atr"], thresholds={"atr": -1.0})
        trades = run_strategy(spec, candles)
        assert all(t["direction"] == "long" for t in trades)

    def test_two_feature_hybrid_requires_both_to_agree(self):
        candles = _candles([100.0] * 20)
        # atr threshold always satisfied (>=0 > -1), but a wildly high
        # vwap_deviation threshold can never be cleared on flat prices --
        # the AND-combinator must produce zero trades.
        spec = _spec(features=["atr", "vwap_deviation"], thresholds={"atr": -1.0, "vwap_deviation": 999.0})
        trades = run_strategy(spec, candles)
        assert trades == []
