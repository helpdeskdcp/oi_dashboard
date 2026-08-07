import datetime as dt

from agents.trading_intelligence import api as ti_api
from agents.trading_intelligence import institutional_intelligence, market_data
from agents.trading_intelligence import ti_store as ts
from test_agents.trading_intelligence.conftest import insert_realistic_chain


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
