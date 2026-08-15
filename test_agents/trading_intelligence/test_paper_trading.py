from agents.trading_intelligence import ai_trading_engine as ate
from agents.trading_intelligence import paper_trading as pt
from agents.trading_intelligence import ti_store as ts
from test_agents.trading_intelligence.conftest import insert_market_structure, insert_realistic_chain


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


class TestRiskGate:
    """Milestone 25 WS3: enter_from_recommendation() now calls
    agents.risk_manager.risk_decision.evaluate_trade_permission() before
    opening a trade -- the account-level daily-loss/exposure gate the M25
    audit found genuinely missing. These tests exercise the real gate
    (not a mock), through the same ti_db fixture every other test in this
    file already uses (see conftest.py's Milestone 25 addition)."""

    def test_normal_conditions_still_open_a_trade_as_before(self, ti_db):
        """No regression for the overwhelmingly common case: an empty,
        healthy account allows entry exactly like it did before this
        milestone."""
        tid = pt.enter_from_recommendation(_rec())
        assert tid is not None

    def test_blocked_when_daily_loss_limit_already_reached(self, ti_db, monkeypatch):
        from agents import config
        from test_agents.risk_manager.conftest import insert_paper_order

        monkeypatch.setattr(config, "RISK_ACCOUNT_CAPITAL", 100_000.0)
        monkeypatch.setattr(config, "RISK_DAILY_LOSS_LIMIT_PCT", 3.0)   # limit = 3,000
        insert_paper_order(ti_db, user_id=1, entry_price=100.0, qty=1, status="CLOSED",
                            points=-5000.0, exit_time="2026-08-15T10:00:00")

        tid = pt.enter_from_recommendation(_rec())
        assert tid is None
        assert ts.list_open_trades(symbol="NIFTY") == []

    def test_blocked_when_symbol_exposure_limit_already_reached(self, ti_db, monkeypatch):
        from agents import config
        from test_agents.risk_manager.conftest import insert_paper_order

        monkeypatch.setattr(config, "RISK_ACCOUNT_CAPITAL", 100_000.0)
        monkeypatch.setattr(config, "RISK_MAX_EXPOSURE_PER_SYMBOL_PCT", 8.0)   # limit = 8,000
        insert_paper_order(ti_db, user_id=1, symbol="NIFTY", entry_price=9000.0, qty=1, status="OPEN")

        tid = pt.enter_from_recommendation(_rec())
        assert tid is None

    def test_a_blocked_entry_publishes_an_explainable_event(self, ti_db, monkeypatch):
        """Never a silent failure -- the block reason is observable via
        the same agents.event_bus every other risk_manager alert already
        uses."""
        from agents import config, event_bus
        from test_agents.risk_manager.conftest import insert_paper_order

        monkeypatch.setattr(config, "RISK_ACCOUNT_CAPITAL", 100_000.0)
        monkeypatch.setattr(config, "RISK_DAILY_LOSS_LIMIT_PCT", 3.0)
        insert_paper_order(ti_db, user_id=1, entry_price=100.0, qty=1, status="CLOSED",
                            points=-5000.0, exit_time="2026-08-15T10:00:00")

        pt.enter_from_recommendation(_rec())
        events = event_bus.events_since("1970-01-01", event_type="entry_blocked_by_risk_gate")
        assert len(events) == 1
        assert events[0]["payload_json"]["symbol"] == "NIFTY"
        assert "daily loss limit" in events[0]["payload_json"]["reason"]

    def test_a_different_symbol_is_not_blocked_by_another_symbols_exposure(self, ti_db, monkeypatch):
        from agents import config
        from test_agents.risk_manager.conftest import insert_paper_order

        monkeypatch.setattr(config, "RISK_ACCOUNT_CAPITAL", 100_000.0)
        monkeypatch.setattr(config, "RISK_MAX_EXPOSURE_PER_SYMBOL_PCT", 8.0)
        insert_paper_order(ti_db, user_id=1, symbol="BANKNIFTY", entry_price=9000.0, qty=1, status="OPEN")

        tid = pt.enter_from_recommendation(_rec())   # _rec() is symbol="NIFTY"
        assert tid is not None


class TestEntryTimeReasoningContextCapture:
    """Milestone 11, Module 11.3: enter_from_recommendation() is the ONE
    place this engine captures regime/timeframe/institutional context AT
    ENTRY -- these tests confirm it's genuinely computed (never
    hardcoded/fabricated), and degrades honestly to None/UNKNOWN when the
    underlying data isn't there."""

    def test_degrades_honestly_with_no_underlying_market_data(self, ti_db):
        """_rec()'s NIFTY has no cycle/market-structure data logged in the
        sqlite fixture at all -- regime (reads market_structure_snapshots)
        and institutional backing (reads the cycles/strikes tables) must
        both come back honestly unavailable, never a fabricated default.
        timeframe_alignment_score_at_entry is NOT expected to be None here
        -- timeframe_confirmation.check() reads the real, always-present
        on-disk NIFTY candle archive (data/history/NIFTY/3m.csv), which is
        independent of this test's sqlite fixture -- see
        test_timeframe_confirmation.py's own TestCheckIntegration for that
        same real-archive convention."""
        tid = pt.enter_from_recommendation(_rec())
        trade = ts.list_open_trades(symbol="NIFTY")[0]
        assert trade["regime_trend_at_entry"] == "UNKNOWN"
        assert trade["institutional_backed_at_entry"] is None

    def test_captures_real_regime_when_market_structure_exists(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        insert_market_structure(ti_db, symbol="NIFTY", ts="2026-08-06T10:00:00", adx=30.0)
        tid = pt.enter_from_recommendation(_rec())
        trade = ts.list_open_trades(symbol="NIFTY")[0]
        assert trade["regime_trend_at_entry"] == "TRENDING"

    def test_accepts_a_prefetched_snapshot_and_findings_without_a_second_fetch(self, ti_db):
        """Dedup discipline: api.run_scheduled_cycle() already has both by
        the time it calls this -- passing them through must produce the
        exact same captured context as the standalone (re-fetching) path."""
        from agents.trading_intelligence import market_data

        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        insert_market_structure(ti_db, symbol="NIFTY", ts="2026-08-06T10:00:00", adx=30.0)
        snapshot = market_data.get_snapshot("NIFTY")

        tid = pt.enter_from_recommendation(_rec(), snapshot=snapshot, findings=[])
        trade = ts.list_open_trades(symbol="NIFTY")[0]
        assert trade["regime_trend_at_entry"] == "TRENDING"
        assert trade["institutional_backed_at_entry"] == 0  # findings=[] passed through -> checked, none found


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

    def test_now_also_includes_sortino_ratio_and_equity_curve(self, ti_db):
        """Milestone 11, Module 11.6: performance_stats() was switched
        from calling backtest.compute_advanced_trade_stats() directly to
        agents.quant_researcher.metrics.compute_stats(), which adds these
        two fields automatically -- every pre-existing field (asserted
        above) stays computed the exact same way."""
        for i in range(3):
            tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                                 target_price=130.0, sl_price=85.0, qty=50)
            ts.close_trade(tid, exit_price=130.0 if i < 2 else 85.0,
                            exit_reason="TARGET HIT" if i < 2 else "STOP LOSS")
        stats = pt.performance_stats(symbol="NIFTY")
        assert "sortino_ratio" in stats
        assert "equity_curve" in stats
        assert len(stats["equity_curve"]) == 3
        assert stats["equity_curve"][-1] == stats["net_pnl"]
