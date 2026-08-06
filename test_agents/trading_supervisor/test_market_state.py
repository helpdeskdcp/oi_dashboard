"""
test_agents/trading_supervisor/test_market_state.py -- regression tests
for market_state.py. backtest.load_market_structure_snapshots is
monkeypatched via a fake module in sys.modules (never a real import of
backtest.py); agents.quant_researcher.data_access.load_candles is
monkeypatched directly -- never a real DB/file access.
"""
import sys
import types

import pandas as pd
import pytest

from agents.quant_researcher import data_access as qr_data_access
from agents.trading_supervisor import market_state


@pytest.fixture()
def fake_backtest(monkeypatch):
    fake = types.ModuleType("backtest")
    monkeypatch.setitem(sys.modules, "backtest", fake)
    return fake


def _vix_candles(levels, *, start="2026-05-01 09:15:00"):
    rows = []
    ts = pd.Timestamp(start)
    for i, level in enumerate(levels):
        rows.append({
            "datetime": ts + pd.Timedelta(days=i), "open": level, "high": level,
            "low": level, "close": level, "volume": 0,
        })
    return pd.DataFrame(rows)


class TestTrendRangeRegime:
    def test_returns_the_logged_regime(self, fake_backtest):
        fake_backtest.load_market_structure_snapshots = lambda symbol, d1, d2: {
            "2026-05-04": {"ts": "t", "market_structure": {"regime": "Trending", "adx": 30.0, "atr_14": 12.0}},
        }
        result = market_state.trend_range_regime("NIFTY", "2026-05-04")
        assert result == {"regime": "Trending", "adx": 30.0, "atr_14": 12.0}

    def test_unknown_when_nothing_logged(self, fake_backtest):
        fake_backtest.load_market_structure_snapshots = lambda symbol, d1, d2: {}
        result = market_state.trend_range_regime("NIFTY", "2026-05-04")
        assert result == {"regime": "unknown", "adx": None, "atr_14": None}

    def test_unknown_on_a_data_access_failure_not_raised(self, fake_backtest):
        def boom(symbol, d1, d2):
            raise RuntimeError("no such table: market_structure_snapshots")

        fake_backtest.load_market_structure_snapshots = boom
        result = market_state.trend_range_regime("NIFTY", "2026-05-04")
        assert result == {"regime": "unknown", "adx": None, "atr_14": None}


class TestVolatilityRegime:
    def test_high_when_current_vix_is_the_highest_in_the_lookback(self, monkeypatch):
        levels = [10.0] * 19 + [30.0]  # current value clearly above the trailing history
        monkeypatch.setattr(qr_data_access, "load_candles", lambda symbol, **k: _vix_candles(levels))
        result = market_state.volatility_regime()
        assert result["level"] == "high"
        assert result["vix"] == 30.0

    def test_low_when_current_vix_is_the_lowest(self, monkeypatch):
        levels = [30.0] * 19 + [5.0]
        monkeypatch.setattr(qr_data_access, "load_candles", lambda symbol, **k: _vix_candles(levels))
        result = market_state.volatility_regime()
        assert result["level"] == "low"

    def test_unknown_with_no_archive(self, monkeypatch):
        monkeypatch.setattr(qr_data_access, "load_candles", lambda symbol, **k: pd.DataFrame())
        result = market_state.volatility_regime()
        assert result["level"] == "unknown"

    def test_unknown_on_a_data_access_failure_not_raised(self, monkeypatch):
        def boom(symbol, **k):
            raise RuntimeError("file corrupted")

        monkeypatch.setattr(qr_data_access, "load_candles", boom)
        result = market_state.volatility_regime()
        assert result["level"] == "unknown"


class TestExpiryRisk:
    def test_unknown_without_a_calendar(self):
        assert market_state.expiry_risk("2026-05-07")["status"] == "unknown"

    def test_today_when_date_is_in_the_calendar(self):
        assert market_state.expiry_risk("2026-05-07", expiry_dates={"2026-05-07"})["status"] == "today"

    def test_tomorrow_when_the_next_day_is_an_expiry(self):
        assert market_state.expiry_risk("2026-05-06", expiry_dates={"2026-05-07"})["status"] == "tomorrow"

    def test_normal_otherwise(self):
        assert market_state.expiry_risk("2026-05-01", expiry_dates={"2026-05-07"})["status"] == "normal"


class TestEventRisk:
    def test_unknown_without_a_calendar(self):
        assert market_state.event_risk("2026-05-07")["status"] == "unknown"

    def test_high_on_an_event_date(self):
        assert market_state.event_risk("2026-05-07", event_dates={"2026-05-07"})["status"] == "high"


class TestAssess:
    def test_combines_all_four_dimensions(self, fake_backtest, monkeypatch):
        fake_backtest.load_market_structure_snapshots = lambda symbol, d1, d2: {}
        monkeypatch.setattr(qr_data_access, "load_candles", lambda symbol, **k: pd.DataFrame())
        state = market_state.assess("NIFTY", "2026-05-04")
        assert state.symbol == "NIFTY"
        assert state.has_unknowns is True

    def test_is_elevated_uncertainty_true_for_high_volatility(self, fake_backtest, monkeypatch):
        fake_backtest.load_market_structure_snapshots = lambda symbol, d1, d2: {
            "2026-05-04": {"ts": "t", "market_structure": {"regime": "Trending", "adx": 30.0, "atr_14": 12.0}},
        }
        monkeypatch.setattr(
            qr_data_access, "load_candles", lambda symbol, **k: _vix_candles([10.0] * 19 + [30.0]),
        )
        state = market_state.assess("NIFTY", "2026-05-04")
        assert state.is_elevated_uncertainty is True
