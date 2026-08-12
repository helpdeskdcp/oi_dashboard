import datetime as dt
import types

from agents.trading_intelligence import ai_trading_engine
from agents.trading_intelligence import api as ti_api
from agents.trading_intelligence import institutional_intelligence, market_data, paper_trading, telegram_notifier
from agents.trading_intelligence import ti_store as ts
from test_agents.trading_intelligence.conftest import insert_realistic_chain


def _make_recommendation(**overrides):
    base = dict(
        symbol="NIFTY", action="BUY CE", direction="CE", strike=24900, market_bias="BULLISH",
        confidence=82, probability=0.6, probability_note="calibrated", risk_score=20,
        entry_price=118.0, sl_price=106.0, target_price=132.0, targets=[132.0, 148.0, 166.0],
        expected_move_pts=50.0, time_horizon="intraday", qty=50, reasoning="Strong bullish setup",
        institutional_reasoning="Institutional buying detected at 24900 CE",
        oi_reasoning="Fresh long buildup, OI supports bulls",
        greeks_reasoning="Delta/Gamma favorable",
        price_action_reasoning="Repeated rejection at support, price holding above VWAP",
    )
    base.update(overrides)
    return ai_trading_engine.Recommendation(**base)


class TestGetSymbolOverview:
    def test_unavailable_symbol(self, ti_db):
        result = ti_api.get_symbol_overview("NOT_A_REAL_SYMBOL")
        assert result["available"] is False

    def test_available_symbol_has_every_documented_section(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY")
        result = ti_api.get_symbol_overview("NIFTY", expiry_date=dt.date.today() + dt.timedelta(days=2))
        assert result["available"] is True
        assert set(result.keys()) >= {
            "symbol", "available", "market_data", "institutional_intelligence",
            "strikes", "recommendation", "timeframes",
        }

    def test_everything_is_json_serializable(self, ti_db):
        import json
        insert_realistic_chain(ti_db, symbol="NIFTY")
        result = ti_api.get_symbol_overview("NIFTY", expiry_date=dt.date.today() + dt.timedelta(days=2))
        json.dumps(result, default=str)  # must not raise

    def test_snapshot_and_analysis_are_each_computed_exactly_once(self, ti_db, monkeypatch):
        """The Priority-1 review's whole point: get_symbol_overview() must
        thread its ONE snapshot/findings read through institutional_intelligence
        and ai_trading_engine, never re-fetching the cycle or re-running the
        full institutional sweep a second (or third) time per call."""
        insert_realistic_chain(ti_db, symbol="NIFTY")

        snapshot_calls, analyze_calls = [], []
        real_get_snapshot = market_data.get_snapshot
        real_analyze = institutional_intelligence.analyze

        def counting_get_snapshot(*args, **kwargs):
            snapshot_calls.append(1)
            return real_get_snapshot(*args, **kwargs)

        def counting_analyze(*args, **kwargs):
            analyze_calls.append(1)
            return real_analyze(*args, **kwargs)

        monkeypatch.setattr(market_data, "get_snapshot", counting_get_snapshot)
        monkeypatch.setattr(institutional_intelligence, "analyze", counting_analyze)

        ti_api.get_symbol_overview("NIFTY", expiry_date=dt.date.today() + dt.timedelta(days=2))

        assert len(snapshot_calls) == 1
        assert len(analyze_calls) == 1


class TestGetPaperTradingSummary:
    def test_reflects_open_and_closed_trades(self, ti_db):
        ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                       target_price=130.0, sl_price=85.0, qty=50)
        summary = ti_api.get_paper_trading_summary()
        assert len(summary["open_trades"]) == 1
        assert summary["stats"]["total_trades"] == 0


class TestGetOverview:
    def test_returns_every_watched_symbol(self, ti_db):
        from agents import config
        insert_realistic_chain(ti_db, symbol="NIFTY")
        overview = ti_api.get_overview()
        assert set(overview["symbols"].keys()) == set(config.TI_WATCHED_SYMBOLS)

    def test_includes_agent_health_and_paper_trading(self, ti_db):
        overview = ti_api.get_overview()
        assert "agent_health" in overview
        assert "paper_trading" in overview

    def test_fully_json_serializable(self, ti_db):
        import json
        insert_realistic_chain(ti_db, symbol="NIFTY")
        overview = ti_api.get_overview()
        json.dumps(overview, default=str)


class TestBuildTelegramPayload:
    """Milestone 19: _build_telegram_payload() maps a real Recommendation
    onto telegram_notifier's payload shape -- never inventing
    institutional_score/premium_momentum/oi_structure/vwap_structure/
    repeated_rejection, since this engine doesn't compute those as
    discrete fields (see telegram_notifier.py's own docstring)."""

    def test_maps_real_recommendation_fields(self):
        rec = _make_recommendation()
        payload = ti_api._build_telegram_payload(rec)

        assert payload["symbol"] == "NIFTY"
        assert payload["signal_type"] == "BUY_CE"
        assert payload["overall_bias"] == "BULLISH"
        assert payload["confidence"] == 82
        assert payload["entry_zone"] == {"strike": 24900, "price": 118.0}
        assert payload["targets"] == [132.0, 148.0, 166.0]
        assert payload["stop_loss"] == 106.0
        assert payload["reasoning"] == "Strong bullish setup"

    def test_never_fabricates_structured_ai_factor_fields(self):
        rec = _make_recommendation()
        payload = ti_api._build_telegram_payload(rec)
        for key in ("institutional_score", "premium_momentum", "oi_structure", "vwap_structure", "repeated_rejection"):
            assert key not in payload

    def test_passes_the_real_reasoning_strings_through_as_details(self):
        rec = _make_recommendation()
        payload = ti_api._build_telegram_payload(rec)
        assert payload["reasoning_details"] == [
            "Institutional buying detected at 24900 CE",
            "Fresh long buildup, OI supports bulls",
            "Delta/Gamma favorable",
            "Repeated rejection at support, price holding above VWAP",
        ]

    def test_omits_empty_reasoning_strings(self):
        rec = _make_recommendation(oi_reasoning="", greeks_reasoning="")
        payload = ti_api._build_telegram_payload(rec)
        assert payload["reasoning_details"] == [
            "Institutional buying detected at 24900 CE",
            "Repeated rejection at support, price holding above VWAP",
        ]


class TestRunScheduledCycleTelegramGate:
    """Milestone 19: run_scheduled_cycle() must call
    telegram_notifier.send_trading_intelligence_signal() exactly once per
    symbol whose Recommendation is an actionable BUY at/above
    config.TI_TELEGRAM_MIN_CONFIDENCE, and never otherwise -- e.g. never
    for HOLD/NO_TRADE, never for a below-threshold BUY, and never merely
    because get_overview()/get_symbol_overview() (a dashboard read) ran."""

    def _wire_single_symbol_cycle(self, monkeypatch, *, rec, ti_db):
        from agents import config
        monkeypatch.setattr(config, "TI_WATCHED_SYMBOLS", ("NIFTY",))
        monkeypatch.setattr(market_data, "get_snapshot", lambda *a, **kw: types.SimpleNamespace(available=True))
        monkeypatch.setattr(institutional_intelligence, "analyze", lambda *a, **kw: {"available": True, "findings": []})
        monkeypatch.setattr(ai_trading_engine, "evaluate", lambda *a, **kw: rec)
        monkeypatch.setattr(paper_trading, "enter_from_recommendation", lambda *a, **kw: 1)
        sent = []
        monkeypatch.setattr(telegram_notifier, "send_trading_intelligence_signal", lambda payload: sent.append(payload) or True)
        return sent

    def test_sends_for_a_high_confidence_actionable_buy(self, ti_db, monkeypatch):
        rec = _make_recommendation(confidence=82)
        sent = self._wire_single_symbol_cycle(monkeypatch, rec=rec, ti_db=ti_db)

        ti_api.run_scheduled_cycle()

        assert len(sent) == 1
        assert sent[0]["symbol"] == "NIFTY"
        assert sent[0]["signal_type"] == "BUY_CE"

    def test_does_not_send_below_the_confidence_threshold(self, ti_db, monkeypatch):
        rec = _make_recommendation(confidence=50)
        sent = self._wire_single_symbol_cycle(monkeypatch, rec=rec, ti_db=ti_db)

        ti_api.run_scheduled_cycle()

        assert sent == []

    def test_does_not_send_for_hold(self, ti_db, monkeypatch):
        rec = _make_recommendation(action="HOLD", direction=None, confidence=90)
        sent = self._wire_single_symbol_cycle(monkeypatch, rec=rec, ti_db=ti_db)

        ti_api.run_scheduled_cycle()

        assert sent == []

    def test_does_not_send_for_no_trade(self, ti_db, monkeypatch):
        rec = _make_recommendation(action="NO_TRADE", direction=None, confidence=None)
        sent = self._wire_single_symbol_cycle(monkeypatch, rec=rec, ti_db=ti_db)

        ti_api.run_scheduled_cycle()

        assert sent == []

    def test_a_plain_dashboard_overview_read_never_sends(self, ti_db, monkeypatch):
        """get_symbol_overview()/get_overview() (a manual dashboard load)
        must never trigger a Telegram signal -- only run_scheduled_cycle()
        (the autonomous/manual-run-cycle path) does. Uses REAL chain data
        (not the stubbed evaluate() the other tests in this class use) so
        this genuinely exercises get_overview()'s real code path end to
        end, confirming the absence of any notifier call isn't just an
        artifact of a mocked evaluate()."""
        insert_realistic_chain(ti_db, symbol="NIFTY")
        sent = []
        monkeypatch.setattr(telegram_notifier, "send_trading_intelligence_signal", lambda payload: sent.append(payload) or True)

        ti_api.get_overview()

        assert sent == []
