"""
test_agents/trading_intelligence/test_ai_live_snapshot.py -- Milestone
21, Phase 3: regression tests for ai_live_snapshot.py, the AI Live
Analysis Snapshot. Mocks multi_timeframe.get_timeframe() and
intelligence_orchestrator.build_snapshot() throughout -- this file
tests the AGGREGATION and pure-math (RSI/EMA) logic, not those other
modules' own internals.
"""
import types

import pandas as pd
import pytest

import intelligence_orchestrator
from agents.trading_intelligence import ai_live_snapshot as als
from agents.trading_intelligence import multi_timeframe
from test_agents.trading_intelligence.conftest import insert_cycle, insert_market_structure, insert_strike


def _candles_df(closes: list) -> pd.DataFrame:
    return pd.DataFrame({
        "datetime": pd.date_range("2026-08-14 09:15", periods=len(closes), freq="3min"),
        "open": closes, "high": closes, "low": closes, "close": closes, "volume": [100] * len(closes),
    })


class TestRSI:
    def test_not_enough_data_returns_none(self):
        assert als._rsi([1.0, 2.0, 3.0]) is None

    def test_all_gains_is_100(self):
        closes = [float(i) for i in range(1, 20)]   # strictly increasing
        assert als._rsi(closes) == 100.0

    def test_all_losses_is_0(self):
        closes = [float(i) for i in range(20, 1, -1)]   # strictly decreasing
        assert als._rsi(closes) == 0.0

    def test_flat_prices_is_neutral(self):
        closes = [100.0] * 20
        # zero gains AND zero losses -- distinct from the all-gains case, which is a real 100
        assert als._rsi(closes) == 50.0


class TestEMA:
    def test_not_enough_data_returns_none(self):
        assert als._latest_ema([1.0, 2.0], 9) is None

    def test_ema_of_constant_series_equals_the_constant(self):
        closes = [50.0] * 15
        assert als._latest_ema(closes, 9) == 50.0

    def test_ema_moves_toward_a_step_change(self):
        closes = [10.0] * 15 + [20.0] * 15
        ema9 = als._latest_ema(closes, 9)
        assert 10.0 < ema9 <= 20.0


class TestBuildAiLiveSnapshot:
    def test_returns_none_with_no_cycle_data(self, ti_db, monkeypatch):
        monkeypatch.setattr(multi_timeframe, "get_timeframe",
                             lambda symbol, tf: {"available": False, "candles": None})
        assert als.build_ai_live_snapshot("NIFTY") is None

    def test_populates_chain_fields_from_the_atm_strike(self, ti_db, monkeypatch):
        cid = insert_cycle(ti_db, symbol="NIFTY", underlying_ltp=24505.0, atm=24500.0, pcr=1.15)
        insert_strike(ti_db, cid, 24500, ce_ltp=120.0, pe_ltp=95.0, ce_oi=50000, pe_oi=60000,
                       ce_oi_chg=1200, pe_oi_chg=-800, ce_delta=0.52, pe_delta=-0.48,
                       ce_theta=-4.1, pe_theta=-3.9, ce_iv=14.2, pe_iv=15.1)
        monkeypatch.setattr(multi_timeframe, "get_timeframe",
                             lambda symbol, tf: {"available": False, "candles": None})
        monkeypatch.setattr(intelligence_orchestrator, "build_snapshot", lambda symbol, **kw: None)

        snap = als.build_ai_live_snapshot("NIFTY")
        assert snap["symbol"] == "NIFTY"
        assert snap["spot_ltp"] == 24505.0
        assert snap["atm_strike"] == 24500.0
        assert snap["ce_ltp"] == 120.0 and snap["pe_ltp"] == 95.0
        assert snap["ce_oi"] == 50000 and snap["pe_oi"] == 60000
        assert snap["ce_oi_chg"] == 1200 and snap["pe_oi_chg"] == -800
        assert snap["pcr"] == 1.15
        assert snap["ce_delta"] == 0.52 and snap["pe_delta"] == -0.48
        assert snap["ce_theta"] == -4.1 and snap["pe_theta"] == -3.9
        assert snap["ce_iv"] == 14.2 and snap["pe_iv"] == 15.1

    def test_pivots_computed_from_stored_pdh_pdl_pdc(self, ti_db, monkeypatch):
        cid = insert_cycle(ti_db, symbol="NIFTY", underlying_ltp=24505.0, atm=24500.0)
        insert_strike(ti_db, cid, 24500)
        insert_market_structure(ti_db, symbol="NIFTY", pdh=24600.0, pdl=24400.0, pdc=24500.0, vwap=24480.0, adx=22.5)
        monkeypatch.setattr(multi_timeframe, "get_timeframe",
                             lambda symbol, tf: {"available": False, "candles": None})
        monkeypatch.setattr(intelligence_orchestrator, "build_snapshot", lambda symbol, **kw: None)

        snap = als.build_ai_live_snapshot("NIFTY")
        assert snap["pivot"] == round((24600.0 + 24400.0 + 24500.0) / 3, 2)
        assert snap["r1"] is not None and snap["s1"] is not None
        assert snap["adx"] == 22.5
        assert snap["vwap_distance"] == round(24505.0 - 24480.0, 2)

    def test_pivots_are_none_without_prior_day_levels(self, ti_db, monkeypatch):
        cid = insert_cycle(ti_db, symbol="NIFTY", underlying_ltp=24505.0, atm=24500.0)
        insert_strike(ti_db, cid, 24500)
        monkeypatch.setattr(multi_timeframe, "get_timeframe",
                             lambda symbol, tf: {"available": False, "candles": None})
        monkeypatch.setattr(intelligence_orchestrator, "build_snapshot", lambda symbol, **kw: None)

        snap = als.build_ai_live_snapshot("NIFTY")
        assert snap["pivot"] is None and snap["r1"] is None and snap["s1"] is None

    def test_oi_walls_pick_the_top_strike_each_side(self, ti_db, monkeypatch):
        cid = insert_cycle(ti_db, symbol="NIFTY", underlying_ltp=24505.0, atm=24500.0)
        insert_strike(ti_db, cid, 24400, ce_oi=10000, pe_oi=90000)   # top PE wall (support)
        insert_strike(ti_db, cid, 24500, ce_oi=20000, pe_oi=20000)
        insert_strike(ti_db, cid, 24600, ce_oi=95000, pe_oi=15000)   # top CE wall (resistance)
        monkeypatch.setattr(multi_timeframe, "get_timeframe",
                             lambda symbol, tf: {"available": False, "candles": None})
        monkeypatch.setattr(intelligence_orchestrator, "build_snapshot", lambda symbol, **kw: None)

        snap = als.build_ai_live_snapshot("NIFTY")
        assert snap["oi_wall_support"] == 24400
        assert snap["oi_wall_resistance"] == 24600

    def test_rsi_and_ema_read_from_multi_timeframe_candles(self, ti_db, monkeypatch):
        cid = insert_cycle(ti_db, symbol="NIFTY", underlying_ltp=24505.0, atm=24500.0)
        insert_strike(ti_db, cid, 24500)
        closes = [float(24000 + i) for i in range(30)]   # strictly increasing -> RSI 100, EMA tracks upward

        def fake_get_timeframe(symbol, tf):
            return {"timeframe": tf, "available": True, "candles": _candles_df(closes), "reason": None}

        monkeypatch.setattr(multi_timeframe, "get_timeframe", fake_get_timeframe)
        monkeypatch.setattr(intelligence_orchestrator, "build_snapshot", lambda symbol, **kw: None)

        snap = als.build_ai_live_snapshot("NIFTY")
        assert snap["rsi_1m"] == 100.0 and snap["rsi_3m"] == 100.0 and snap["rsi_5m"] == 100.0
        assert snap["ema_9"] is not None and snap["ema_21"] is not None
        assert snap["ema_9"] > snap["ema_21"]   # faster EMA leads in a sustained uptrend

    def test_institutional_bias_and_confidence_from_intelligence_orchestrator(self, ti_db, monkeypatch):
        cid = insert_cycle(ti_db, symbol="NIFTY", underlying_ltp=24505.0, atm=24500.0)
        insert_strike(ti_db, cid, 24500)
        monkeypatch.setattr(multi_timeframe, "get_timeframe",
                             lambda symbol, tf: {"available": False, "candles": None})
        fake = types.SimpleNamespace(to_dict=lambda: {"bias": "BEARISH", "confidence": 61})
        monkeypatch.setattr(intelligence_orchestrator, "build_snapshot", lambda symbol, **kw: fake)

        snap = als.build_ai_live_snapshot("NIFTY")
        assert snap["institutional_bias"] == "BEARISH"
        assert snap["ai_confidence"] == 61

    def test_a_build_snapshot_failure_is_handled_honestly(self, ti_db, monkeypatch):
        cid = insert_cycle(ti_db, symbol="NIFTY", underlying_ltp=24505.0, atm=24500.0)
        insert_strike(ti_db, cid, 24500)
        monkeypatch.setattr(multi_timeframe, "get_timeframe",
                             lambda symbol, tf: {"available": False, "candles": None})

        def _raise(*a, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr(intelligence_orchestrator, "build_snapshot", _raise)
        snap = als.build_ai_live_snapshot("NIFTY")
        assert snap["institutional_bias"] is None
        assert snap["ai_confidence"] is None


class TestToTelegramText:
    def test_none_snapshot_is_a_clear_message(self):
        assert als.to_telegram_text(None) == "No live snapshot available."

    def test_a_real_snapshot_renders_every_field_once(self, ti_db, monkeypatch):
        cid = insert_cycle(ti_db, symbol="NIFTY", underlying_ltp=24505.0, atm=24500.0, pcr=1.1)
        insert_strike(ti_db, cid, 24500, ce_ltp=120.0, pe_ltp=95.0)
        monkeypatch.setattr(multi_timeframe, "get_timeframe",
                             lambda symbol, tf: {"available": False, "candles": None})
        monkeypatch.setattr(intelligence_orchestrator, "build_snapshot", lambda symbol, **kw: None)

        snap = als.build_ai_live_snapshot("NIFTY")
        text = als.to_telegram_text(snap)
        assert "NIFTY" in text
        assert "120.0" in text and "95.0" in text
        assert "\n" in text   # multi-line, not a single wall of text
