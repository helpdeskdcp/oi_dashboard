import sqlite3

from agents.trading_intelligence import ai_trading_engine as ate
from agents.trading_intelligence import explainability as ex
from agents.trading_intelligence import regime_profile as rp
from agents.trading_intelligence import timeframe_confirmation as tc
from agents.trading_intelligence import ti_store as ts
from test_agents.trading_intelligence.conftest import insert_cycle, insert_market_structure, insert_realistic_chain, insert_strike


def _rec(action="BUY CE", direction=None, confidence=80, probability=None, probability_note="x",
         institutional_reasoning="No institutional-intelligence findings at this strike this cycle."):
    return ate.Recommendation(
        symbol="NIFTY", action=action, direction=direction if direction else ("CE" if "CE" in action else None),
        strike=24500, market_bias="BULLISH",
        confidence=confidence, probability=probability, probability_note=probability_note, risk_score=20,
        entry_price=100.0, sl_price=80.0, target_price=140.0, targets=[140.0, 160.0], expected_move_pts=50.0,
        time_horizon="3 day(s) to expiry", qty=50, reasoning="test",
        institutional_reasoning=institutional_reasoning, oi_reasoning="x", greeks_reasoning="x",
        price_action_reasoning="x",
    )


class TestExplainRecommendationBasics:
    def test_no_trade_is_honest_and_never_fetches_regime(self, ti_db):
        rec = _rec(action="NO_TRADE", direction=None, institutional_reasoning="")
        result = ex.explain_recommendation(rec)
        assert "NO_TRADE" in result.summary
        assert "regime" not in result.inputs_used
        assert "timeframe_alignment" not in result.inputs_used

    def test_hold_mentions_the_open_position(self, ti_db):
        rec = _rec(action="HOLD", direction="CE", institutional_reasoning="")
        result = ex.explain_recommendation(rec)
        assert "HOLD" in result.summary

    def test_buy_includes_confidence_and_risk_score(self, ti_db):
        rec = _rec(institutional_reasoning="")
        result = ex.explain_recommendation(rec)
        assert "confidence 80/100" in result.summary
        assert "risk score 20/100" in result.summary
        assert "confidence" in result.inputs_used

    def test_probability_present_is_cited(self, ti_db):
        rec = _rec(probability=66.7, institutional_reasoning="")
        result = ex.explain_recommendation(rec)
        assert "66.7% win rate" in result.summary
        assert "probability" in result.inputs_used

    def test_probability_absent_cites_the_honest_note(self, ti_db):
        rec = _rec(probability=None, probability_note="insufficient history", institutional_reasoning="")
        result = ex.explain_recommendation(rec)
        assert "insufficient history" in result.summary
        assert "probability" not in result.inputs_used

    def test_institutional_reasoning_text_is_always_included_when_non_empty(self, ti_db):
        rec = _rec(institutional_reasoning="Long Buildup detected at 24500 CE.")
        result = ex.explain_recommendation(rec)
        assert "Long Buildup detected at 24500 CE." in result.summary
        assert "institutional_reasoning" in result.inputs_used

    def test_empty_institutional_reasoning_is_omitted(self, ti_db):
        rec = _rec(institutional_reasoning="")
        result = ex.explain_recommendation(rec)
        assert "institutional_reasoning" not in result.inputs_used


class TestExplainRecommendationRegimeAndAlignment:
    def test_regime_included_when_real_data_shows_a_trend(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        insert_market_structure(ti_db, symbol="NIFTY", ts="2026-08-06T10:00:00", adx=30.0)
        rec = _rec(institutional_reasoning="")
        result = ex.explain_recommendation(rec)
        assert "trending" in result.summary.lower()
        assert "regime" in result.inputs_used

    def test_regime_omitted_when_genuinely_unavailable(self, ti_db):
        rec = _rec(institutional_reasoning="")
        result = ex.explain_recommendation(rec)
        assert "regime" not in result.inputs_used

    def test_institutional_persistence_cited_when_buildup_is_sustained(self, ti_db):
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_signal='Long Buildup' WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        for i in range(3):
            older_cid = insert_cycle(ti_db, symbol="NIFTY", ts=f"2026-08-06T09:{10 + i}:00",
                                      underlying_ltp=24505.0, atm=24500.0)
            insert_strike(ti_db, older_cid, 24500, ce_signal="Long Buildup")

        rec = _rec(institutional_reasoning="")
        result = ex.explain_recommendation(rec)
        assert "persisted for 4 consecutive cycles" in result.summary
        assert "institutional_persistence" in result.inputs_used
        # No market_structure/ADX data was inserted in this test -- the
        # trend regime itself is UNKNOWN, but persistence (sourced from
        # strikes history, not ADX) must still be cited independently.
        assert "regime" not in result.inputs_used

    def test_timeframe_alignment_cited_against_the_real_archive(self, ti_db):
        """timeframe_confirmation.check() reads the real, always-present
        on-disk NIFTY candle archive -- independent of the ti_db sqlite
        fixture -- so this must be present even with zero chain data."""
        rec = _rec(institutional_reasoning="")
        result = ex.explain_recommendation(rec)
        assert "Timeframe confirmation is" in result.summary
        assert "timeframe_alignment" in result.inputs_used

    def test_accepts_prefetched_regime_and_alignment(self, ti_db):
        regime = rp.classify("NIFTY")
        alignment = tc.check("NIFTY", direction="CE")
        rec = _rec(institutional_reasoning="")
        result = ex.explain_recommendation(rec, regime=regime, alignment=alignment)
        assert result.summary  # ran without a second fetch, no exception


class TestExplainRecommendationDeterminism:
    def test_same_inputs_produce_identical_output(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        insert_market_structure(ti_db, symbol="NIFTY", ts="2026-08-06T10:00:00", adx=30.0)
        rec = _rec(probability=60.0)
        first = ex.explain_recommendation(rec)
        second = ex.explain_recommendation(rec)
        assert first == second

    def test_never_raises_across_a_range_of_actions_and_symbols(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        for action, direction in (("BUY CE", "CE"), ("BUY PE", "PE"), ("HOLD", "CE"), ("NO_TRADE", None)):
            for symbol in ("NIFTY", "NOT_A_REAL_SYMBOL"):
                rec = _rec(action=action, direction=direction)
                rec = ate.Recommendation(**{**rec.__dict__, "symbol": symbol})
                result = ex.explain_recommendation(rec)
                assert result.symbol == symbol
                assert result.summary


class TestExplainTradeQuality:
    def test_open_trade_is_explained_honestly_without_a_score(self, ti_db):
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=130.0, sl_price=85.0, qty=50)
        trade = ts.list_open_trades(symbol="NIFTY")[0]
        result = ex.explain_trade_quality(trade)
        assert "not closed yet" in result.summary
        assert result.inputs_used == []

    def test_closed_trade_with_no_captured_context_explains_the_gap(self, ti_db):
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=130.0, sl_price=85.0, qty=50)
        ts.close_trade(tid, exit_price=130.0, exit_reason="TARGET HIT")
        trade = ts.list_closed_trades(symbol="NIFTY")[0]
        result = ex.explain_trade_quality(trade)
        assert "won" in result.summary
        assert "Trade Quality Score unavailable" in result.summary
        assert "trade_quality" not in result.inputs_used

    def test_closed_trade_with_full_context_cites_every_component(self, ti_db):
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=130.0, sl_price=85.0, qty=50,
                             regime_trend_at_entry="TRENDING", timeframe_alignment_score_at_entry=75.0,
                             institutional_backed_at_entry=True)
        ts.close_trade(tid, exit_price=130.0, exit_reason="TARGET HIT")
        trade = ts.list_closed_trades(symbol="NIFTY")[0]
        result = ex.explain_trade_quality(trade)
        assert "Trade Quality Score:" in result.summary
        assert "Regime component" in result.summary
        assert "Timeframe alignment component" in result.summary
        assert "institutional finding backed" in result.summary
        assert set(result.inputs_used) == {"outcome", "trade_quality", "regime", "timeframe_alignment", "institutional_persistence"}

    def test_a_loss_is_worded_differently_from_a_win(self, ti_db):
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=130.0, sl_price=85.0, qty=50)
        ts.close_trade(tid, exit_price=85.0, exit_reason="STOP LOSS")
        trade = ts.list_closed_trades(symbol="NIFTY")[0]
        result = ex.explain_trade_quality(trade)
        assert "lost" in result.summary
        assert "won" not in result.summary

    def test_not_backed_institutional_component_is_worded_honestly(self, ti_db):
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=130.0, sl_price=85.0, qty=50, institutional_backed_at_entry=False)
        ts.close_trade(tid, exit_price=130.0, exit_reason="TARGET HIT")
        trade = ts.list_closed_trades(symbol="NIFTY")[0]
        result = ex.explain_trade_quality(trade)
        assert "did not back" in result.summary


class TestExplainTradeQualityDeterminism:
    def test_same_trade_dict_produces_identical_output(self, ti_db):
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=130.0, sl_price=85.0, qty=50, regime_trend_at_entry="TRENDING")
        ts.close_trade(tid, exit_price=130.0, exit_reason="TARGET HIT")
        trade = ts.list_closed_trades(symbol="NIFTY")[0]
        assert ex.explain_trade_quality(trade) == ex.explain_trade_quality(trade)

    def test_never_raises_across_a_lifecycle_of_mixed_trades(self, ti_db):
        for i in range(8):
            has_context = i % 2 == 0
            tid = ts.open_trade(
                symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                target_price=130.0, sl_price=85.0, qty=50,
                regime_trend_at_entry="TRENDING" if has_context else None,
                timeframe_alignment_score_at_entry=75.0 if has_context else None,
                institutional_backed_at_entry=(i % 3 == 0) if has_context else None,
            )
            ts.close_trade(tid, exit_price=130.0 if i < 5 else 85.0,
                            exit_reason="TARGET HIT" if i < 5 else "STOP LOSS")
        for trade in ts.list_closed_trades(symbol="NIFTY", limit=100):
            result = ex.explain_trade_quality(trade)
            assert result.summary
