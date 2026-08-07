import pytest

from agents.trading_intelligence import ti_store as ts


class TestOpenAndCloseTrade:
    def test_open_trade_appears_in_open_list(self, ti_db):
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=130.0, sl_price=85.0, qty=50)
        open_trades = ts.list_open_trades(symbol="NIFTY")
        assert len(open_trades) == 1
        assert open_trades[0]["id"] == tid
        assert open_trades[0]["status"] == "OPEN"

    def test_close_trade_computes_points_correctly_for_a_win(self, ti_db):
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=130.0, sl_price=85.0, qty=50)
        result = ts.close_trade(tid, exit_price=130.0, exit_reason="TARGET HIT")
        assert result["points"] == 1500.0  # (130-100)*50
        assert result["status"] == "CLOSED"

    def test_close_trade_computes_points_correctly_for_a_loss(self, ti_db):
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=130.0, sl_price=85.0, qty=50)
        result = ts.close_trade(tid, exit_price=85.0, exit_reason="STOP LOSS")
        assert result["points"] == -750.0  # (85-100)*50

    def test_closed_trade_no_longer_in_open_list(self, ti_db):
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=130.0, sl_price=85.0, qty=50)
        ts.close_trade(tid, exit_price=130.0, exit_reason="TARGET HIT")
        assert ts.list_open_trades(symbol="NIFTY") == []
        assert len(ts.list_closed_trades(symbol="NIFTY")) == 1

    def test_close_unknown_trade_raises(self, ti_db):
        with pytest.raises(ValueError):
            ts.close_trade(99999, exit_price=100.0, exit_reason="TARGET HIT")

    def test_open_trades_filtered_by_symbol(self, ti_db):
        ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                       target_price=130.0, sl_price=85.0, qty=50)
        ts.open_trade(symbol="BANKNIFTY", strike=51000, direction="PE", entry_price=200.0,
                       target_price=170.0, sl_price=220.0, qty=15)
        assert len(ts.list_open_trades(symbol="NIFTY")) == 1
        assert len(ts.list_open_trades()) == 2


class TestEntryTimeReasoningContext:
    """Milestone 11, Module 11.3: open_trade()'s new optional kwargs --
    additive, defaulting to None so every pre-existing caller/test above
    is unaffected."""

    def test_defaults_to_none_when_not_supplied(self, ti_db):
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=130.0, sl_price=85.0, qty=50)
        trade = ts.list_open_trades(symbol="NIFTY")[0]
        assert trade["regime_trend_at_entry"] is None
        assert trade["regime_volatility_at_entry"] is None
        assert trade["timeframe_alignment_score_at_entry"] is None
        assert trade["institutional_backed_at_entry"] is None

    def test_context_roundtrips_through_open_and_close(self, ti_db):
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=130.0, sl_price=85.0, qty=50,
                             regime_trend_at_entry="TRENDING", regime_volatility_at_entry="HIGH",
                             timeframe_alignment_score_at_entry=83.3, institutional_backed_at_entry=True)
        open_trade = ts.list_open_trades(symbol="NIFTY")[0]
        assert open_trade["regime_trend_at_entry"] == "TRENDING"
        assert open_trade["regime_volatility_at_entry"] == "HIGH"
        assert open_trade["timeframe_alignment_score_at_entry"] == 83.3
        assert open_trade["institutional_backed_at_entry"] == 1  # SQLite has no bool -- stored as 0/1

        closed = ts.close_trade(tid, exit_price=130.0, exit_reason="TARGET HIT")
        assert closed["regime_trend_at_entry"] == "TRENDING"
        assert closed["institutional_backed_at_entry"] == 1

    def test_institutional_backed_false_is_stored_as_zero_not_null(self, ti_db):
        ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                       target_price=130.0, sl_price=85.0, qty=50, institutional_backed_at_entry=False)
        trade = ts.list_open_trades(symbol="NIFTY")[0]
        assert trade["institutional_backed_at_entry"] == 0


class TestSignalLog:
    def test_record_and_list_signal(self, ti_db):
        ts.record_signal(symbol="NIFTY", action="NO_TRADE", reasoning="neutral bias")
        signals = ts.list_signals(symbol="NIFTY")
        assert len(signals) == 1
        assert signals[0]["action"] == "NO_TRADE"

    def test_findings_json_roundtrips(self, ti_db):
        ts.record_signal(symbol="NIFTY", action="BUY CE", findings=[{"pattern_type": "OIWall", "strike": 24500}])
        signals = ts.list_signals(symbol="NIFTY")
        assert signals[0]["findings_json"][0]["pattern_type"] == "OIWall"
