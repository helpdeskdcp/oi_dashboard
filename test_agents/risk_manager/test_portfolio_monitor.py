"""
test_agents/risk_manager/test_portfolio_monitor.py -- regression tests
for the Live Portfolio Risk Monitor. Uses the paper_db fixture (real-
shaped schema, never app.py) plus the shared agent_db fixture so
agents.event_bus writes somewhere throwaway too.
"""
import agents.quant_researcher.data_access as qr_data_access
from agents.risk_manager import data_access, portfolio_monitor
from .conftest import insert_engine_trade, insert_paper_order, insert_strikes, insert_user


def _position(**kwargs):
    defaults = dict(
        source="paper_orders", id=1, user_id=1, symbol="NIFTY", strike=22000, direction="CE",
        entry_price=100.0, sl_price=80.0, target_price=140.0, qty=50, entry_time="t",
    )
    defaults.update(kwargs)
    return data_access.Position(**defaults)


class TestPositionRiskAndNotional:
    def test_risk_uses_stop_distance_when_sl_is_set(self):
        p = _position(entry_price=100.0, sl_price=80.0, qty=50)
        assert portfolio_monitor._position_risk(p) == 1000.0  # |100-80| * 50

    def test_risk_falls_back_to_full_premium_without_a_stop(self):
        p = _position(entry_price=100.0, sl_price=None, qty=50)
        assert portfolio_monitor._position_risk(p) == 5000.0  # 100 * 50

    def test_notional_is_entry_times_qty(self):
        p = _position(entry_price=100.0, qty=50)
        assert portfolio_monitor._position_notional(p) == 5000.0


class TestComputeExposureHeatMargin:
    def test_exposure_sums_notional(self):
        positions = [_position(entry_price=100.0, qty=50), _position(entry_price=50.0, qty=20)]
        assert portfolio_monitor.compute_exposure(positions) == 6000.0

    def test_heat_is_risk_over_capital(self):
        positions = [_position(entry_price=100.0, sl_price=80.0, qty=50)]  # risk 1000
        heat = portfolio_monitor.compute_portfolio_heat(positions, capital=10_000)
        assert heat == 10.0

    def test_heat_is_zero_with_no_capital(self):
        assert portfolio_monitor.compute_portfolio_heat([_position()], capital=0) == 0.0

    def test_margin_utilization_reconstructs_total_capital(self):
        positions = [_position(entry_price=100.0, qty=50)]  # notional 5000
        util = portfolio_monitor.compute_margin_utilization(positions, wallet_balance=5000.0)
        assert util == 50.0  # 5000 / (5000+5000)


class TestConcentration:
    def test_splits_by_symbol_as_percentages(self):
        positions = [
            _position(symbol="NIFTY", entry_price=100.0, qty=50),   # 5000
            _position(symbol="BANKNIFTY", entry_price=100.0, qty=50),  # 5000
        ]
        conc = portfolio_monitor.compute_concentration(positions)
        assert conc == {"NIFTY": 50.0, "BANKNIFTY": 50.0}

    def test_empty_positions_returns_empty(self):
        assert portfolio_monitor.compute_concentration([]) == {}


class TestGreeksExposure:
    def test_sums_available_greeks_weighted_by_qty(self, paper_db):
        insert_strikes(paper_db, "NIFTY", 22000, ce_delta=0.5, ce_gamma=0.02)
        positions = [_position(symbol="NIFTY", strike=22000, direction="CE", qty=50)]
        greeks = portfolio_monitor.compute_greeks_exposure(positions)
        assert greeks["delta"] == 25.0  # 0.5 * 50
        assert greeks["gamma"] == 1.0
        assert greeks["theta"] is None  # never logged -- None, not 0.0
        assert greeks["vega"] is None

    def test_no_resolvable_strike_returns_all_none(self, paper_db):
        positions = [_position(symbol="NIFTY", strike=99999, direction="CE")]
        greeks = portfolio_monitor.compute_greeks_exposure(positions)
        assert greeks == {"delta": None, "gamma": None, "theta": None, "vega": None}


class TestSymbolPriceCorrelation:
    def test_none_when_no_candle_archive(self, monkeypatch):
        monkeypatch.setattr(qr_data_access, "load_candles", lambda symbol, **k: __import__("pandas").DataFrame())
        assert portfolio_monitor.symbol_price_correlation("NIFTY", "BANKNIFTY") is None


class TestSnapshot:
    def test_no_positions_produces_a_clean_snapshot(self, paper_db, agent_db):
        insert_user(paper_db, 1, wallet_balance=100_000.0)
        result = portfolio_monitor.snapshot(user_id=1)
        assert result.exposure == 0.0
        assert result.alerts == []
        assert "0 open position" in result.summary

    def test_high_heat_produces_an_alert_and_publishes_to_event_bus(self, paper_db, agent_db, monkeypatch):
        from agents import config, event_bus
        insert_user(paper_db, 1, wallet_balance=100.0)  # tiny wallet -> heat blows past the limit
        insert_paper_order(paper_db, user_id=1, entry_price=100.0, sl_price=0.0, qty=50, status="OPEN")
        monkeypatch.setattr(config, "RISK_PORTFOLIO_HEAT_LIMIT_PCT", 1.0)

        result = portfolio_monitor.snapshot(user_id=1)

        assert any(a.metric == "portfolio_heat" for a in result.alerts)
        events = event_bus.events_since("2000-01-01")
        assert any(e["event_type"] == "risk_alert" for e in events)

    def test_recommendations_are_never_empty_for_a_critical_alert(self, paper_db, agent_db, monkeypatch):
        from agents import config
        insert_user(paper_db, 1, wallet_balance=100.0)
        insert_paper_order(paper_db, user_id=1, entry_price=100.0, sl_price=0.0, qty=50, status="OPEN")
        monkeypatch.setattr(config, "RISK_PORTFOLIO_HEAT_LIMIT_PCT", 1.0)

        result = portfolio_monitor.snapshot(user_id=1)
        heat_alert = next(a for a in result.alerts if a.metric == "portfolio_heat")
        assert heat_alert.recommendation
