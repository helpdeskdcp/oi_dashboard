from agents.trading_intelligence import ai_trading_engine as ate
from agents.trading_intelligence import paper_trading as pt
from agents.trading_intelligence import ti_store as ts


def _rec(action="BUY CE", qty=50):
    return ate.Recommendation(
        symbol="NIFTY", action=action, direction="CE" if "CE" in action else "PE", strike=24500,
        market_bias="BULLISH",
        confidence=80, probability=None, probability_note="x", risk_score=20, entry_price=100.0, sl_price=80.0,
        target_price=140.0, targets=[140.0, 160.0], expected_move_pts=50.0, time_horizon="3 day(s) to expiry",
        qty=qty, reasoning="test", institutional_reasoning="x", oi_reasoning="x", greeks_reasoning="x",
        price_action_reasoning="x",
    )


class TestEnterFromRecommendation:
    def test_opens_a_trade_for_a_buy_recommendation(self, ti_db):
        tid = pt.enter_from_recommendation(_rec())
        assert tid is not None
        assert len(ts.list_open_trades(symbol="NIFTY")) == 1

    def test_opened_trade_carries_the_real_strike_so_it_can_later_auto_close(self, ti_db):
        """Regression test for a real bug caught during Priority-5 review:
        enter_from_recommendation() used to hardcode strike=None, which
        meant _check_open_trade_exit() could never match the trade against
        a future cycle's snapshot.strikes -- the trade would sit open
        forever, no matter what the market did."""
        tid = pt.enter_from_recommendation(_rec())
        trade = ts.list_open_trades(symbol="NIFTY")[0]
        assert trade["strike"] == 24500

    def test_no_op_for_no_trade(self, ti_db):
        rec = ate.Recommendation(symbol="NIFTY", action="NO_TRADE", direction=None, strike=None, market_bias=None,
                                  confidence=None, probability=None, probability_note="x", risk_score=None,
                                  entry_price=None, sl_price=None, target_price=None, targets=[],
                                  expected_move_pts=None, time_horizon="n/a", qty=None, reasoning="x",
                                  institutional_reasoning="", oi_reasoning="", greeks_reasoning="",
                                  price_action_reasoning="")
        assert pt.enter_from_recommendation(rec) is None

    def test_no_op_for_hold(self, ti_db):
        rec = ate.Recommendation(symbol="NIFTY", action="HOLD", direction="CE", strike=24500,
                                  market_bias="BULLISH",
                                  confidence=70, probability=None, probability_note="x", risk_score=20,
                                  entry_price=100.0, sl_price=80.0, target_price=140.0, targets=[140.0],
                                  expected_move_pts=None, time_horizon="3 day(s) to expiry", qty=50,
                                  reasoning="x", institutional_reasoning="", oi_reasoning="", greeks_reasoning="",
                                  price_action_reasoning="", open_trade_id=1)
        assert pt.enter_from_recommendation(rec) is None

    def test_no_op_when_quantity_sizes_to_zero(self, ti_db):
        assert pt.enter_from_recommendation(_rec(qty=0)) is None


class TestCloseAndJournal:
    def test_closes_the_trade_and_writes_a_journal_entry(self, ti_db, memory_store):
        tid = pt.enter_from_recommendation(_rec())
        trade = pt.close_and_journal(tid, exit_price=140.0, exit_reason="TARGET HIT", memory_store=memory_store,
                                      learning="worked well")
        assert trade["status"] == "CLOSED"
        journal = memory_store.search_trade_journal(symbol="NIFTY")
        assert len(journal) == 1
        assert journal[0]["learning"] == "worked well"
        assert "TARGET HIT" in journal[0]["actual_result"]


class TestPerformanceStats:
    def test_empty_when_no_trades(self, ti_db):
        stats = pt.performance_stats()
        assert stats["total_trades"] == 0

    def test_reflects_real_closed_trades(self, ti_db):
        for i in range(3):
            tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                                 target_price=130.0, sl_price=85.0, qty=50)
            ts.close_trade(tid, exit_price=130.0 if i < 2 else 85.0,
                            exit_reason="TARGET HIT" if i < 2 else "STOP LOSS")
        stats = pt.performance_stats(symbol="NIFTY")
        assert stats["total_trades"] == 3
        assert stats["wins"] == 2
        assert stats["losses"] == 1
