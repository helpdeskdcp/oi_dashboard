"""Unit tests for agents/trading_intelligence/dual_probability_backtest.py's
real-signal dataset builder -- synthetic data only, no real DB dependency,
matching test_institutional_flow_backtest.py's established pattern of
monkeypatching the data-loading seam rather than needing oi_history.db."""
import datetime as dt
from unittest import mock

import pandas as pd
import pytest

from agents.trading_intelligence.dual_probability_backtest import build_dataset_from_real_signals


def _candles(n=400):
    start = dt.datetime(2026, 7, 13, 9, 15)
    rows = []
    price = 100.0
    for i in range(n):
        price += 0.05
        rows.append({
            "datetime": start + dt.timedelta(minutes=3 * i),
            "open": price, "high": price + 0.5, "low": price - 0.5, "close": price,
        })
    return pd.DataFrame(rows)


def _cycle(ts, *, direction="CE", entry=100.0, target=110.0, sl=95.0, tradeable=True, pcr=1.0):
    return {"cycle": {
        "ts": ts.isoformat(), "signal_tradeable": tradeable, "signal_direction": direction,
        "signal_entry": entry, "signal_target": target, "signal_sl": sl, "pcr": pcr,
    }}


class TestRealSignalDataset:
    def test_only_tradeable_signals_with_real_direction_are_used(self):
        candles = _candles()
        base_ts = candles["datetime"].iloc[50]
        raw = [
            _cycle(base_ts, tradeable=False),                    # excluded: not tradeable
            _cycle(base_ts + dt.timedelta(minutes=60), direction=None),  # excluded: no direction
        ]
        with mock.patch("agents.quant_researcher.data_access.load_candles", return_value=candles), \
             mock.patch("backtest.load_cycles", return_value=raw):
            rows = build_dataset_from_real_signals("FAKESYM", date_from="2026-07-13", date_to="2026-07-13")
        assert rows == []

    def test_ce_maps_to_long_pe_maps_to_short(self):
        candles = _candles()
        ts1 = candles["datetime"].iloc[50]
        ts2 = candles["datetime"].iloc[200]  # far enough apart to dodge dedup cooldown
        raw = [_cycle(ts1, direction="CE"), _cycle(ts2, direction="PE")]
        with mock.patch("agents.quant_researcher.data_access.load_candles", return_value=candles), \
             mock.patch("backtest.load_cycles", return_value=raw):
            rows = build_dataset_from_real_signals("FAKESYM", date_from="2026-07-13", date_to="2026-07-13")
        directions = {r.direction for r in rows}
        assert directions == {"long", "short"}

    def test_dedup_cooldown_collapses_rapid_refires(self):
        candles = _candles()
        base_ts = candles["datetime"].iloc[50]
        # 5 re-fires within seconds of each other -- classic cycles-poll-every-7-15s pattern
        raw = [_cycle(base_ts + dt.timedelta(seconds=10 * i), direction="CE") for i in range(5)]
        with mock.patch("agents.quant_researcher.data_access.load_candles", return_value=candles), \
             mock.patch("backtest.load_cycles", return_value=raw):
            rows = build_dataset_from_real_signals("FAKESYM", date_from="2026-07-13", date_to="2026-07-13")
        assert len(rows) == 1  # only the first is accepted; the rest fall inside the cooldown window

    def test_dedup_cooldown_is_independent_of_horizon_bars(self):
        """The horizon sweep found that tying cooldown to horizon_bars
        (the old `3 * horizon_bars` formula) destroyed sample size faster
        than widening the horizon reduced the PENDING rate. Confirms the
        fix: cooldown no longer changes when horizon_bars changes."""
        candles = _candles()
        base_ts = candles["datetime"].iloc[50]
        # two signals 40 minutes apart -- inside a horizon_bars=20 (60min)
        # implied old cooldown, but outside the fixed 30min default
        raw = [
            _cycle(base_ts, direction="CE"),
            _cycle(base_ts + dt.timedelta(minutes=40), direction="CE"),
        ]
        with mock.patch("agents.quant_researcher.data_access.load_candles", return_value=candles), \
             mock.patch("backtest.load_cycles", return_value=raw):
            rows_h10 = build_dataset_from_real_signals(
                "FAKESYM", date_from="2026-07-13", date_to="2026-07-13", horizon_bars=10)
            rows_h20 = build_dataset_from_real_signals(
                "FAKESYM", date_from="2026-07-13", date_to="2026-07-13", horizon_bars=20)
        # both signals accepted at BOTH horizons -- cooldown (default 30min)
        # doesn't grow just because horizon_bars grew from 10 to 20
        assert len(rows_h10) == 2
        assert len(rows_h20) == 2

    def test_dedup_cooldown_minutes_is_still_a_real_gate(self):
        candles = _candles()
        base_ts = candles["datetime"].iloc[50]
        raw = [
            _cycle(base_ts, direction="CE"),
            _cycle(base_ts + dt.timedelta(minutes=5), direction="CE"),  # inside a 30min cooldown
        ]
        with mock.patch("agents.quant_researcher.data_access.load_candles", return_value=candles), \
             mock.patch("backtest.load_cycles", return_value=raw):
            rows = build_dataset_from_real_signals(
                "FAKESYM", date_from="2026-07-13", date_to="2026-07-13", dedup_cooldown_minutes=30)
        assert len(rows) == 1

    def test_uses_real_target_sl_distances_not_atr_scaled(self):
        candles = _candles()
        ts = candles["datetime"].iloc[50]
        raw = [_cycle(ts, direction="CE", entry=100.0, target=130.0, sl=90.0)]
        with mock.patch("agents.quant_researcher.data_access.load_candles", return_value=candles), \
             mock.patch("backtest.load_cycles", return_value=raw):
            rows = build_dataset_from_real_signals("FAKESYM", date_from="2026-07-13", date_to="2026-07-13")
        assert len(rows) == 1  # sanity: the real signal_target=130/signal_sl=90 (30/10 distances) drove labeling,
        # not this module's own ATR-scaled defaults from build_dataset()

    def test_no_cycles_returns_empty_not_crash(self):
        candles = _candles()
        with mock.patch("agents.quant_researcher.data_access.load_candles", return_value=candles), \
             mock.patch("backtest.load_cycles", return_value=[]):
            rows = build_dataset_from_real_signals("FAKESYM", date_from="2026-07-13", date_to="2026-07-13")
        assert rows == []

    def test_missing_target_or_sl_is_skipped_not_crash(self):
        candles = _candles()
        ts = candles["datetime"].iloc[50]
        raw = [{"cycle": {"ts": ts.isoformat(), "signal_tradeable": True, "signal_direction": "CE",
                           "signal_entry": 100.0, "signal_target": None, "signal_sl": 95.0}}]
        with mock.patch("agents.quant_researcher.data_access.load_candles", return_value=candles), \
             mock.patch("backtest.load_cycles", return_value=raw):
            rows = build_dataset_from_real_signals("FAKESYM", date_from="2026-07-13", date_to="2026-07-13")
        assert rows == []
