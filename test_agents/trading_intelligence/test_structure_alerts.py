"""
test_agents/trading_intelligence/test_structure_alerts.py -- Milestone 20,
Phase 2: regression tests for agents/trading_intelligence/structure_alerts.py,
the STRUCTURE-ALERT-ONLY wiring of institutional_levels.py into the live
Trading Intelligence cycle. Pure unit tests -- no DB, no real HTTP/candle
fetch (everything monkeypatched), matching this package's own convention
of never importing app.py from test_agents/.
"""
import datetime as dt
import types

import pytest

from agents import config
from agents.runtime import market_session
from agents.trading_intelligence import data_access, market_data, structure_alerts as sa, telegram_notifier


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    sa._last_alert_by_key.clear()
    monkeypatch.setattr(config, "TI_ENABLE_STRUCTURE_ALERTS", True)
    yield
    sa._last_alert_by_key.clear()


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


def _mock_single_level(monkeypatch, *, weight=0.9):
    """weighted_levels() clustering itself is institutional_levels.py's
    own tested responsibility (test_institutional_levels.py) -- these
    orchestration tests fix its output to one deterministic level so
    they only exercise structure_alerts.py's own gating/dedup/wiring
    logic, not the full clustering pipeline."""
    monkeypatch.setattr(sa.il, "weighted_levels",
                         lambda *a, **kw: [{"level": LEVEL, "type": "RESISTANCE", "weight": weight, "sources": ["TEST"]}])


class TestEvaluateSymbolGating:
    def test_returns_not_evaluated_when_flag_is_off(self, monkeypatch):
        monkeypatch.setattr(config, "TI_ENABLE_STRUCTURE_ALERTS", False)
        result = sa.evaluate_symbol("NIFTY")
        assert result == {"symbol": "NIFTY", "evaluated": False, "reason": "TI_ENABLE_STRUCTURE_ALERTS is False"}

    def test_returns_not_evaluated_when_market_closed(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (False, "Weekend"))
        result = sa.evaluate_symbol("NIFTY")
        assert result["evaluated"] is False
        assert "market closed" in result["reason"]

    def test_returns_not_evaluated_when_snapshot_unavailable(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        result = sa.evaluate_symbol("NIFTY", snapshot=_snapshot(available=False, reason="no cycle logged"))
        assert result["evaluated"] is False
        assert "no OI snapshot" in result["reason"]

    def test_returns_not_evaluated_when_no_candle_data(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        result = sa.evaluate_symbol("NIFTY", snapshot=_snapshot(), candles=[])
        assert result["evaluated"] is False
        assert "no candle data" in result["reason"]

    def test_never_fetches_candles_or_snapshot_when_flag_is_off(self, monkeypatch):
        monkeypatch.setattr(config, "TI_ENABLE_STRUCTURE_ALERTS", False)
        calls = []
        monkeypatch.setattr(market_data, "get_snapshot", lambda *a, **kw: calls.append("snapshot"))
        monkeypatch.setattr(data_access, "load_candles", lambda *a, **kw: calls.append("candles"))
        sa.evaluate_symbol("NIFTY")
        assert calls == []


class TestEvaluateSymbolAlerting:
    def test_sends_a_structure_alert_for_a_real_reversal(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        sent = []
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda payload: sent.append(payload) or True)

        result = sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert result["evaluated"] is True
        assert len(sent) >= 1
        alert = sent[0]
        assert alert["symbol"] == "NIFTY"
        assert alert["current_role"] == "SUPPORT"

    def test_never_calls_the_trade_signal_sender(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        signal_calls = []
        monkeypatch.setattr(telegram_notifier, "send_trading_intelligence_signal", lambda p: signal_calls.append(p) or True)
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda payload: True)

        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES)

        assert signal_calls == []

    def test_duplicate_alert_within_15_minutes_is_suppressed(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        sent = []
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda payload: sent.append(payload) or True)

        now = dt.datetime(2026, 8, 10, 12, 0)
        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES, now=now)
        first_count = len(sent)
        assert first_count >= 1

        later = now + dt.timedelta(minutes=10)
        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES, now=later)
        assert len(sent) == first_count  # no new send -- still within the 15-minute window

    def test_alert_after_15_minutes_is_not_suppressed(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        _mock_single_level(monkeypatch)
        sent = []
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda payload: sent.append(payload) or True)

        now = dt.datetime(2026, 8, 10, 12, 0)
        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES, now=now)
        first_count = len(sent)

        later = now + dt.timedelta(minutes=16)
        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=24540), candles=REVERSAL_CANDLES, now=later)
        assert len(sent) > first_count

    def test_a_flat_uninteresting_level_sends_nothing(self, monkeypatch):
        monkeypatch.setattr(market_session, "is_exchange_open", lambda ex, **kw: (True, ""))
        sent = []
        monkeypatch.setattr(telegram_notifier, "send_structure_update", lambda payload: sent.append(payload) or True)
        flat_candles = [_candle(i, 100, 100.1, 99.9, 100) for i in range(60)]

        sa.evaluate_symbol("NIFTY", snapshot=_snapshot(underlying=100, vwap=100), candles=flat_candles)

        assert sent == []


class TestRunStructureAlertCycle:
    def test_returns_empty_dict_immediately_when_flag_is_off(self, monkeypatch):
        monkeypatch.setattr(config, "TI_ENABLE_STRUCTURE_ALERTS", False)
        calls = []
        monkeypatch.setattr(market_session, "active_symbols", lambda symbols, **kw: calls.append(1) or [])
        assert sa.run_structure_alert_cycle() == {}
        assert calls == []

    def test_evaluates_only_the_given_symbols(self, monkeypatch):
        seen = []
        monkeypatch.setattr(sa, "evaluate_symbol", lambda sym, **kw: seen.append(sym) or {"symbol": sym, "evaluated": False, "reason": "x"})
        sa.run_structure_alert_cycle(symbols=["CRUDEOIL", "GOLD"])
        assert seen == ["CRUDEOIL", "GOLD"]

    def test_defaults_to_active_symbols_when_none_given(self, monkeypatch):
        monkeypatch.setattr(market_session, "active_symbols", lambda symbols, **kw: ["NATURALGAS"])
        seen = []
        monkeypatch.setattr(sa, "evaluate_symbol", lambda sym, **kw: seen.append(sym) or {"symbol": sym, "evaluated": False, "reason": "x"})
        sa.run_structure_alert_cycle()
        assert seen == ["NATURALGAS"]
