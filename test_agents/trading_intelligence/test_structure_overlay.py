"""
test_agents/trading_intelligence/test_structure_overlay.py -- Milestone 20,
Phase 5: regression tests for agents/trading_intelligence/structure_overlay.py,
the read-only "what is this symbol's structure doing right now" query
backing the dashboard's Structure Overlay panel. Pure unit tests -- no DB,
no real HTTP/candle fetch (everything monkeypatched), matching
structure_alerts.py's own test conventions.
"""
import datetime as dt
import types

import pytest

from agents.runtime import market_session
from agents.trading_intelligence import data_access, market_data, structure_overlay as so, telegram_notifier


def _snapshot(*, available=True, reason=None, strikes=None, atm=100, underlying=103, vwap=100):
    return types.SimpleNamespace(available=available, reason=reason, strikes=strikes or [],
                                  atm=atm, underlying_ltp=underlying, vwap=vwap)


def _candle(minute, o, h, l, c, v=1000):
    base = dt.datetime(2026, 8, 10, 9, 0)
    return {"datetime": base + dt.timedelta(minutes=minute), "open": o, "high": h, "low": l, "close": c, "volume": v}


LEVEL = 24500  # NIFTY's real profile (breakout_buffer=20, retest_tolerance=5) --
# scale the candle data to actually clear those real thresholds.
REVERSAL_CANDLES = [
    _candle(0, 24480, 24540, 24470, 24535),   # close 24535 > 24500+20
    _candle(3, 24530, 24545, 24503, 24540),   # retest low 24503 <= 24505, big lower wick, close above
]


def _mock_single_level(monkeypatch, *, weight=0.9, level_type="RESISTANCE"):
    monkeypatch.setattr(so.il, "weighted_levels",
                         lambda *a, **kw: [{"level": LEVEL, "type": level_type, "weight": weight, "sources": ["TEST"]}])


class TestComputeOverlayGating:
    def test_returns_not_available_when_market_closed(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (False, "Weekend"))
        result = so.compute_overlay("NIFTY")
        assert result["available"] is False
        assert "market closed" in result["reason"]

    def test_returns_not_available_when_snapshot_unavailable(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        result = so.compute_overlay("NIFTY", snapshot=_snapshot(available=False, reason="no cycle logged"))
        assert result["available"] is False
        assert "no OI snapshot" in result["reason"]

    def test_returns_not_available_when_no_candle_data(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        result = so.compute_overlay("NIFTY", snapshot=_snapshot(), candles=[])
        assert result["available"] is False
        assert "no candle data" in result["reason"]

    def test_is_not_gated_by_the_structure_alerts_flag(self, monkeypatch):
        # Deliberately different from structure_alerts.evaluate_symbol():
        # this is a plain read, not a Telegram send, so it must still
        # work even when TI_ENABLE_STRUCTURE_ALERTS is False.
        from agents import config
        monkeypatch.setattr(config, "TI_ENABLE_STRUCTURE_ALERTS", False)
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)

        result = so.compute_overlay("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert result["available"] is True

    def test_never_sends_telegram(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        calls = []
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda *a, **kw: calls.append(1) or True)
        monkeypatch.setattr(telegram_notifier, "send_trading_intelligence_signal", lambda *a, **kw: calls.append(1) or True)

        so.compute_overlay("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert calls == []

    def test_never_opens_a_paper_trade(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        from agents.trading_intelligence import paper_trading
        calls = []
        monkeypatch.setattr(paper_trading, "enter_from_recommendation", lambda *a, **kw: calls.append(1))

        so.compute_overlay("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert calls == []


class TestComputeOverlayReversal:
    def test_reports_state_and_confidence_for_a_real_reversal(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)

        result = so.compute_overlay("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert result["available"] is True
        assert result["state"] == "BULLISH_RETEST_ACTIVE"
        assert result["is_major_level"] is True
        assert result["current_role"] == "SUPPORT"
        assert result["previous_role"] == "RESISTANCE"
        assert result["confidence"] > 0

    def test_support_is_the_flipped_level_for_a_bullish_reversal(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)

        result = so.compute_overlay("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert result["support"] == LEVEL
        assert result["level"] == LEVEL

    def test_overlay_trade_plan_is_attached_for_high_confidence(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)

        result = so.compute_overlay("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert result["overlay"] == {"direction": "BULLISH", "entry": 24545, "sl": 24498, "t1": 24592, "t2": 24639}

    def test_no_overlay_key_when_confidence_is_too_low(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        monkeypatch.setattr(so.il, "compute_trade_plan_overlay", lambda *a, **kw: None)

        result = so.compute_overlay("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert "overlay" not in result

    def test_reversal_support_and_resistance_come_from_other_weighted_levels(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        monkeypatch.setattr(so.il, "weighted_levels", lambda *a, **kw: [
            {"level": LEVEL, "type": "RESISTANCE", "weight": 0.9, "sources": ["TEST"]},
            {"level": LEVEL - 100, "type": "SUPPORT", "weight": 0.7, "sources": ["TEST"]},
            {"level": LEVEL + 150, "type": "RESISTANCE", "weight": 0.7, "sources": ["TEST"]},
        ])

        result = so.compute_overlay("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert result["reversal_support"] == LEVEL - 100
        assert result["reversal_resistance"] == LEVEL + 150
        assert result["resistance"] == LEVEL + 150  # bullish -> the other side is the nearer resistance above


class TestComputeOverlayFallback:
    def test_falls_back_to_best_candidate_level_when_nothing_is_major(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        monkeypatch.setattr(so.il, "weighted_levels", lambda *a, **kw: [])
        monkeypatch.setattr(so.il, "best_candidate_level", lambda *a, **kw: {
            "level": 8000.0, "type": "RESISTANCE", "weight": 0.35, "sources": ["OI_WALL_CE", "ROUND_NUMBER"], "is_major": False,
        })

        result = so.compute_overlay("CRUDEOIL", snapshot=_snapshot(), candles=REVERSAL_CANDLES)

        assert result["available"] is True
        assert result["state"] == "NO_MAJOR_LEVEL"
        assert result["is_major_level"] is False
        assert result["confidence"] == 35
        assert result["level"] == 8000.0
        assert result["resistance"] == 8000.0
        assert "overlay" not in result

    def test_reports_no_candidate_when_genuinely_nothing_exists(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        monkeypatch.setattr(so.il, "weighted_levels", lambda *a, **kw: [])
        monkeypatch.setattr(so.il, "best_candidate_level", lambda *a, **kw: None)

        result = so.compute_overlay("CRUDEOIL", snapshot=_snapshot(), candles=REVERSAL_CANDLES)

        assert result["available"] is True
        assert result["state"] is None


class TestComputeOverlayFlatMarket:
    def test_a_flat_uninteresting_level_still_returns_a_state(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        flat_candles = [_candle(i, 100, 100.1, 99.9, 100) for i in range(60)]

        result = so.compute_overlay("NIFTY", snapshot=_snapshot(underlying=100, vwap=100), candles=flat_candles)

        assert result["available"] is True
        assert result["state"] in (None, so.NO_MAJOR_LEVEL, "RANGE")


class TestComputeOverlayDefaultFetch:
    def test_fetches_snapshot_and_candles_when_not_given(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        monkeypatch.setattr(market_data, "get_snapshot", lambda sym: _snapshot(underlying=24540))
        import pandas as pd
        df = pd.DataFrame(REVERSAL_CANDLES)
        monkeypatch.setattr(data_access, "load_candles", lambda sym, **kw: df)
        _mock_single_level(monkeypatch)

        result = so.compute_overlay("NIFTY")

        assert result["available"] is True
