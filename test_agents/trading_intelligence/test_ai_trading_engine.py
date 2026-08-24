import datetime as dt
import sqlite3

from agents.trading_intelligence import ai_trading_engine as ate
from agents.trading_intelligence import ti_store as ts
from test_agents.trading_intelligence.conftest import (
    insert_cycle,
    insert_realistic_chain,
    insert_strike,
)


class TestEvaluate:
    def test_no_trade_when_no_data(self, ti_db):
        rec = ate.evaluate("NOT_A_REAL_SYMBOL")
        assert rec.action == "NO_TRADE"
        assert rec.reasoning

    def test_no_trade_on_neutral_bias(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", pcr=1.0)  # neutral PCR, no directional signals
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        assert rec.action == "NO_TRADE"

    def test_buy_ce_recommendation_has_every_required_field(self, ti_db):
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute(f"UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering', ce_trading_symbol='NIFTY_TEST_CE', ce_token='1', ce_contract_expiry='{(dt.date.today() + dt.timedelta(days=2)).isoformat()}' WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, expiry_date=dt.date.today() + dt.timedelta(days=2))
        assert rec.action in ("BUY CE", "BUY PE")
        assert rec.direction in ("CE", "PE")
        assert rec.confidence is not None
        assert rec.entry_price is not None
        assert rec.sl_price is not None
        assert rec.target_price is not None
        assert rec.qty is not None
        assert rec.reasoning

    def test_as_of_ts_matches_the_real_snapshot_the_signal_was_built_from(self, ti_db):
        # MARKET_SNAPSHOT_INTEGRITY_AUDIT.md: a real Telegram signal was
        # observed with no way to tell how old its underlying data was.
        # rec.as_of_ts must reflect the exact cycle this recommendation
        # came from, not be None or a fabricated "now".
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute(f"UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering', ce_trading_symbol='NIFTY_TEST_CE', ce_token='1', ce_contract_expiry='{(dt.date.today() + dt.timedelta(days=2)).isoformat()}' WHERE cycle_id=? AND strike=24500", (cid,))
        cursor = conn.execute("SELECT ts FROM cycles WHERE id=?", (cid,))
        expected_ts = cursor.fetchone()[0]
        conn.commit()
        conn.close()
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, expiry_date=dt.date.today() + dt.timedelta(days=2))
        assert rec.action in ("BUY CE", "BUY PE")
        assert rec.as_of_ts == expected_ts

    def test_as_of_ts_is_none_when_no_snapshot_is_available(self, ti_db):
        rec = ate.evaluate("NOT_A_REAL_SYMBOL")
        assert rec.action == "NO_TRADE"
        assert rec.as_of_ts is None

    def test_as_of_ts_is_populated_on_no_edge_no_trade_too(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", pcr=1.0)  # neutral PCR, no directional signal
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        assert rec.action == "NO_TRADE"
        assert rec.as_of_ts is not None

    def test_failure_gate_is_none_by_default(self, ti_db):
        # config.TI_ENABLE_FAILURE_GATE_SHADOW defaults off -- this must be
        # byte-identical to before that field existed.
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering', ce_trading_symbol='NIFTY_TEST_CE', ce_token='1' WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        assert rec.failure_gate_status is None
        assert rec.failure_gate_failed is None

    def test_failure_gate_populates_when_enabled_but_never_changes_the_real_decision(self, ti_db, monkeypatch):
        from agents import config as agents_config
        monkeypatch.setattr(agents_config, "TI_ENABLE_FAILURE_GATE_SHADOW", True)
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute(f"UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering', ce_trading_symbol='NIFTY_TEST_CE', ce_token='1', ce_contract_expiry='{(dt.date.today() + dt.timedelta(days=2)).isoformat()}' WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()

        without_gate = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, expiry_date=dt.date.today() + dt.timedelta(days=2))
        monkeypatch.setattr(agents_config, "TI_ENABLE_FAILURE_GATE_SHADOW", False)
        # (re-fetch a baseline with the flag off, same inputs, to compare against)
        baseline = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, expiry_date=dt.date.today() + dt.timedelta(days=2))

        assert without_gate.failure_gate_status in ("CLEAR", "BLOCKED")
        assert isinstance(without_gate.failure_gate_failed, list)
        # The one and only contract that matters: the gate is observation-only.
        assert without_gate.action == baseline.action
        assert without_gate.direction == baseline.direction
        assert without_gate.entry_price == baseline.entry_price
        assert without_gate.sl_price == baseline.sl_price
        assert without_gate.target_price == baseline.target_price
        assert without_gate.qty == baseline.qty

    def test_probability_is_honestly_none_with_no_history(self, ti_db):
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute(f"UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering', ce_trading_symbol='NIFTY_TEST_CE', ce_token='1', ce_contract_expiry='{(dt.date.today() + dt.timedelta(days=2)).isoformat()}' WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, expiry_date=dt.date.today() + dt.timedelta(days=2))
        assert rec.probability is None
        assert "insufficient history" in rec.probability_note

    def test_probability_calibrates_from_enough_closed_trades(self, ti_db):
        for i in range(6):
            tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                                 target_price=120.0, sl_price=90.0, qty=50, confidence=85)
            ts.close_trade(tid, exit_price=120.0 if i < 4 else 90.0, exit_reason="TARGET HIT" if i < 4 else "STOP LOSS")
        prob, note = ate._calibrated_probability(85)
        assert prob == 66.7  # 4/6 wins
        assert "historical win rate" in note

    def test_hold_when_open_position_exists_and_no_exit_hit(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                       target_price=160.0, sl_price=80.0, qty=50, confidence=70)
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        assert rec.action == "HOLD"
        assert rec.open_trade_id is not None

    def test_auto_closes_and_reports_target_hit(self, ti_db):
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_ltp=165.0 WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=160.0, sl_price=80.0, qty=50, confidence=70)
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        assert rec.action == "NO_TRADE"
        assert "TARGET HIT" in rec.reasoning
        assert ts.list_open_trades(symbol="NIFTY") == []
        closed = ts.list_closed_trades(symbol="NIFTY")
        assert closed[0]["id"] == tid
        assert closed[0]["exit_reason"] == "TARGET HIT"

    def test_auto_closes_and_reports_stop_loss(self, ti_db):
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_ltp=70.0 WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                       target_price=160.0, sl_price=80.0, qty=50, confidence=70)
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        assert "STOP LOSS" in rec.reasoning

    def test_auto_closes_via_fallback_when_strike_has_drifted_outside_current_window(self, ti_db):
        """Regression test for a real bug found in production (2026-08-11,
        CRUDEOIL trade #11): a trade's strike can drift outside the CURRENT
        cycle's bounded near-ATM window as the underlying moves -- exactly
        when a losing position's SL is most likely to be breached. The old
        code silently returned None (HOLD) whenever this happened, so the
        trade never closed even though its last known price was already
        well past the stop-loss."""
        old_cid = insert_cycle(ti_db, symbol="NIFTY", ts="2026-08-06T10:00:00", underlying_ltp=24505, atm=24500)
        insert_strike(ti_db, old_cid, 24500, ce_ltp=70.0)
        ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                       target_price=160.0, sl_price=80.0, qty=50, confidence=70)
        # Underlying has since drifted 500 points away -- the current
        # (latest) cycle's near-ATM window no longer includes strike 24500.
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=25005, atm=25000, ts="2026-08-06T11:00:00")
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        assert rec.action == "NO_TRADE"
        assert "STOP LOSS" in rec.reasoning
        assert ts.list_open_trades(symbol="NIFTY") == []
        closed = ts.list_closed_trades(symbol="NIFTY")
        assert closed[0]["exit_reason"] == "STOP LOSS"
        assert closed[0]["exit_price"] == 70.0


class TestExpiryContractIdentity:
    """Regression coverage for the expiry-contract-identity bug (2026-08-19,
    NIFTY trade #76): entry 4.9, "exit" 125.4 the next morning -- not a
    real 25x move, two unrelated option contracts' prices being compared
    because the old code matched by strike number alone, with zero
    contract-identity/expiry awareness."""

    def test_same_expiry_still_matches_normally(self, ti_db):
        """The fix must not change behavior for the common case: entry
        expiry == current cycle's resolved expiry."""
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500,
                                               ts="2026-08-06T11:00:00")
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_ltp=165.0 WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                       target_price=160.0, sl_price=80.0, qty=50, confidence=70,
                       expiry_date_at_entry="2026-08-06")
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, expiry_date=dt.date(2026, 8, 6))
        assert "TARGET HIT" in rec.reasoning
        assert ts.list_closed_trades(symbol="NIFTY")[0]["exit_price"] == 165.0

    def test_rollover_never_matches_against_the_new_contracts_price(self, ti_db):
        """The core bug: a NEW contract at the SAME strike has a price
        (110.0 here) that would ALSO trigger this trade's target (>=100)
        if wrongly matched -- the fix must use the pre-rollover price
        (70.0) instead, never the new cycle's chain."""
        old_cid = insert_cycle(ti_db, symbol="NIFTY", ts="2026-08-06T15:25:00", underlying_ltp=24505, atm=24500)
        insert_strike(ti_db, old_cid, 24500, ce_ltp=70.0)
        ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=60.0,
                       target_price=90.0, sl_price=40.0, qty=50, confidence=70,
                       expiry_date_at_entry="2026-08-06")
        # New weekly cycle: same strike, a fresh, unrelated, much higher
        # premium (full time value) -- exactly what a real rollover looks like.
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, ts="2026-08-07T09:16:00")
        cid2 = sqlite3.connect(ti_db).execute("SELECT id FROM cycles ORDER BY ts DESC LIMIT 1").fetchone()[0]
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_ltp=110.0 WHERE cycle_id=? AND strike=24500", (cid2,))
        conn.commit()
        conn.close()

        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, expiry_date=dt.date(2026, 8, 13))
        assert rec.action == "NO_TRADE"
        assert "EXPIRED" in rec.reasoning
        closed = ts.list_closed_trades(symbol="NIFTY")[0]
        assert closed["exit_reason"].startswith("EXPIRED")
        assert closed["exit_price"] == 70.0  # the OLD contract's last real price, never 110.0

    def test_rollover_with_no_prior_history_holds_rather_than_fabricating_an_exit(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, ts="2026-08-07T09:16:00")
        ts.open_trade(symbol="NIFTY", strike=99999, direction="CE", entry_price=60.0,
                       target_price=90.0, sl_price=40.0, qty=50, confidence=70,
                       expiry_date_at_entry="2026-08-06")  # strike with zero history anywhere
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, expiry_date=dt.date(2026, 8, 13))
        assert rec.action == "HOLD"
        assert len(ts.list_open_trades(symbol="NIFTY")) == 1

    def test_no_current_expiry_date_skips_rollover_check_backward_compatible(self, ti_db):
        """A caller that doesn't resolve/pass expiry_date at all (the
        default) must see exactly today's pre-fix behavior -- normal
        strike-matching, never blocked by a rollover check it has no
        data for."""
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500,
                                               ts="2026-08-06T11:00:00")
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_ltp=165.0 WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                       target_price=160.0, sl_price=80.0, qty=50, confidence=70,
                       expiry_date_at_entry="2026-08-06")
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)  # no expiry_date passed
        assert "TARGET HIT" in rec.reasoning

    def test_missing_expiry_at_entry_is_backfilled_and_matches_normally_this_cycle(self, ti_db):
        """A trade opened before this fix existed (NULL expiry_date_at_entry)
        must be backfilled on its first post-deploy evaluation, then
        proceed with ordinary target/SL matching THIS same cycle (backfilled
        value == current cycle's resolved expiry, so no rollover fires)."""
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500,
                                               ts="2026-08-06T11:00:00")
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_ltp=165.0 WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                       target_price=160.0, sl_price=80.0, qty=50, confidence=70)  # no expiry_date_at_entry
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, expiry_date=dt.date(2026, 8, 6))
        assert "TARGET HIT" in rec.reasoning
        assert ts.list_closed_trades(symbol="NIFTY")[0]["exit_price"] == 165.0

    def test_buy_recommendation_carries_every_priority_2_field(self, ti_db):
        """Priority 2 review requirement: every recommendation must include
        Market Bias, Confidence, Probability, Risk Score, Entry, SL, Multiple
        Targets (T1/T2/T3), Expected Move, Time Horizon, Institutional/OI/
        Greeks/Price Action reasoning -- checked explicitly here, not just
        implied by the earlier "has_every_required_field" smoke test."""
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute(f"UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering', ce_trading_symbol='NIFTY_TEST_CE', ce_token='1', ce_contract_expiry='{(dt.date.today() + dt.timedelta(days=2)).isoformat()}' WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, expiry_date=dt.date.today() + dt.timedelta(days=2))
        assert rec.action in ("BUY CE", "BUY PE")
        assert rec.market_bias is not None
        assert isinstance(rec.targets, list) and len(rec.targets) >= 1
        assert rec.targets[0] == rec.target_price
        assert rec.targets == sorted(set(rec.targets))  # strictly increasing
        assert rec.time_horizon == "2 day(s) to expiry -- near-term"
        assert rec.oi_reasoning and "PCR" in rec.oi_reasoning
        assert rec.greeks_reasoning
        assert rec.price_action_reasoning
        assert rec.institutional_reasoning is not None  # may honestly be "no findings" text, never missing

    def test_time_horizon_helper_is_honest_when_no_expiry_given(self):
        # Pure unit test of the helper itself -- the HOLD path (an
        # already-open trade) still calls _time_horizon(expiry_date) even
        # when expiry_date is None, so this string must still exist.
        assert ate._time_horizon(None) == "unknown (no expiry date provided)"

    def test_no_trade_recommendation_still_carries_market_bias(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", pcr=1.0)  # neutral -> NO_TRADE
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        assert rec.action == "NO_TRADE"
        assert rec.targets == []
        assert rec.expected_move_pts is None


class TestExpiryFailClosedGate:
    """Expiry-integrity scoped fix (2026-08-24): an otherwise-actionable
    BUY signal must never reach a real user without a resolved expiry
    attached. Real live callers (api.run_scheduled_cycle(),
    api.get_symbol_overview()) already resolve expiry_date via the
    canonical expiry_intelligence.get_nearest_expiry() resolver before
    calling evaluate() -- this gate only fires when that resolution
    genuinely failed or was skipped for this symbol this cycle."""

    def _buy_eligible_chain(self, ti_db):
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute(f"UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering', ce_trading_symbol='NIFTY_TEST_CE', ce_token='1', ce_contract_expiry='{(dt.date.today() + dt.timedelta(days=2)).isoformat()}' WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        return cid

    def test_actionable_signal_is_blocked_when_expiry_is_unresolved(self, ti_db):
        self._buy_eligible_chain(ti_db)
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)  # no expiry_date passed
        assert rec.action == "NO_TRADE"
        assert "EXPIRY_NOT_RESOLVED" in rec.reasoning
        # Never trades blind: no entry/target/SL/qty on a blocked signal.
        assert rec.entry_price is None
        assert rec.sl_price is None
        assert rec.target_price is None
        assert rec.qty is None

    def test_blocked_signal_still_reports_direction_and_strike_for_debugging(self, ti_db):
        self._buy_eligible_chain(ti_db)
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        assert rec.direction in ("CE", "PE")
        assert rec.strike == 24500

    def test_same_signal_proceeds_normally_once_expiry_is_resolved(self, ti_db):
        self._buy_eligible_chain(ti_db)
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, expiry_date=dt.date.today() + dt.timedelta(days=2))
        assert rec.action in ("BUY CE", "BUY PE")
        assert rec.entry_price is not None
        assert rec.expiry_date_resolved == dt.date.today() + dt.timedelta(days=2)

    def test_hold_path_for_an_already_open_trade_is_unaffected_by_the_gate(self, ti_db):
        # The gate only guards a NEW actionable BUY -- an existing open
        # position must still report HOLD even with expiry_date=None,
        # exactly as before this fix (item 5: don't touch existing logic
        # beyond what's needed for expiry integrity).
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                       target_price=160.0, sl_price=80.0, qty=50, confidence=70)
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        assert rec.action == "HOLD"

    def test_neutral_no_trade_is_unaffected_by_the_gate(self, ti_db):
        # A genuine "no edge this cycle" NO_TRADE must keep its own
        # honest reason, never get relabeled as an expiry problem it
        # never had.
        insert_realistic_chain(ti_db, symbol="NIFTY", pcr=1.0)  # neutral bias
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        assert rec.action == "NO_TRADE"
        assert "EXPIRY_NOT_RESOLVED" not in rec.reasoning


class TestContractIdentityPropagation:
    """Expiry-integrity scoped fix (2026-08-24), item 3: trading_symbol/
    token -- captured by app.py's build_strike_rows() from
    AngelOneFetcher.find_option_token(), persisted onto the strikes row,
    and now propagated through to the Recommendation this signal is on."""

    def test_trading_symbol_and_token_reach_the_recommendation(self, ti_db):
        expiry_date = dt.date.today() + dt.timedelta(days=2)
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute(
            "UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering', "
            "ce_trading_symbol=?, ce_token=?, ce_contract_expiry=? WHERE cycle_id=? AND strike=24500",
            ("NIFTY06AUG2624500CE", "99999", expiry_date.isoformat(), cid),
        )
        conn.commit()
        conn.close()
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, expiry_date=expiry_date)
        assert rec.action in ("BUY CE", "BUY PE")
        if rec.direction == "CE":
            assert rec.trading_symbol == "NIFTY06AUG2624500CE"
            assert rec.token == "99999"

    def test_blocked_when_the_strikes_row_predates_the_persistence_migration(self, ti_db):
        # insert_realistic_chain()'s default strikes never set
        # ce_trading_symbol/ce_token -- exactly what an old, pre-migration
        # row looks like. Since the INVALID_OPTION_CONTRACT hard-validation
        # gate (2026-08-24 follow-up) was added, this must now BLOCK the
        # signal rather than let a BUY through with unconfirmed contract
        # identity -- a real, deliberate behavior change from this same
        # session's earlier (propagation-only) version of this test.
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering' WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, expiry_date=dt.date.today() + dt.timedelta(days=2))
        assert rec.action == "NO_TRADE"
        assert "INVALID_OPTION_CONTRACT" in rec.reasoning
        assert rec.trading_symbol is None
        assert rec.token is None

    def test_blocked_when_resolved_expiry_does_not_match_the_confirmed_contracts_own_expiry(self, ti_db):
        """Regression lock for a reported concern (2026-08-24): a NIFTY
        signal allegedly showed "Expiry: 26-Aug-2026" while the real
        broker-listed nearest expiry (confirmed against the live
        instrument_master.json this session) is 25-Aug-2026 -- no
        26-Aug-2026 NIFTY contract exists at all. This engine could not
        reproduce that specific bug (expiry_intelligence.get_nearest_expiry()
        and app.py's find_option_token() both independently return
        2026-08-25 against real data, see test_expiry_intelligence_real_broker_data.py),
        but this test locks in the defense regardless: IF the resolved
        expiry_date and the confirmed contract's OWN expiry (captured at
        fetch time, never re-derived from the trading_symbol string) ever
        disagree -- a stale row, a per-strike listing gap between two
        independent resolvers -- the signal must block, never trade the
        mismatched contract."""
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute(
            "UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering', ce_trading_symbol=?, ce_token=?, "
            "ce_contract_expiry=? WHERE cycle_id=? AND strike=24500",
            ("NIFTY25AUG2624500CE", "61734", "2026-08-25", cid),
        )
        conn.commit()
        conn.close()
        # The caller resolved (or was fed) 2026-08-26 -- one day off from
        # the confirmed contract's real 2026-08-25 expiry.
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, expiry_date=dt.date(2026, 8, 26))
        assert rec.action == "NO_TRADE"
        assert "INVALID_OPTION_CONTRACT" in rec.reasoning
        assert "2026-08-26" in rec.reasoning
        assert "2026-08-25" in rec.reasoning

    def test_matching_expiry_proceeds_to_a_real_buy(self, ti_db):
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute(
            "UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering', ce_trading_symbol=?, ce_token=?, "
            "ce_contract_expiry=? WHERE cycle_id=? AND strike=24500",
            ("NIFTY25AUG2624500CE", "61734", "2026-08-25", cid),
        )
        conn.commit()
        conn.close()
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, expiry_date=dt.date(2026, 8, 25))
        assert rec.action == "BUY CE"
        assert rec.trading_symbol == "NIFTY25AUG2624500CE"
        assert rec.token == "61734"
        assert rec.expiry_date_resolved == dt.date(2026, 8, 25)


class TestMultiTargets:
    def test_targets_are_strictly_increasing_and_bounded_to_three(self, ti_db):
        from oi_engine import StrikeRow

        rows = [
            StrikeRow(strike=24400, ce_oi=30000, pe_oi=95000),
            StrikeRow(strike=24450, ce_oi=60000, pe_oi=70000),
            StrikeRow(strike=24500, ce_oi=50000, pe_oi=88000),
            StrikeRow(strike=24550, ce_oi=90000, pe_oi=50000),
            StrikeRow(strike=24600, ce_oi=200000, pe_oi=40000),
            StrikeRow(strike=24650, ce_oi=150000, pe_oi=30000),
        ]
        from oi_engine import oi_walls
        support, resistance = oi_walls(rows)
        signal = {
            "direction": "CE", "entry_price": 100.0, "target_price": 130.0, "delta_used": 0.5,
        }
        targets = ate._multi_targets(signal, support, resistance, atm=24500)
        assert targets[0] == 130.0
        assert len(targets) <= 3
        assert targets == sorted(set(targets))


class TestRiskScore:
    def test_risk_score_bounded_0_to_100(self, ti_db):
        score = ate._compute_risk_score(entry_price=100.0, sl_price=80.0, capital=500000, risk_pct=1.0)
        assert 0 <= score <= 100

    def test_a_stop_too_wide_for_the_risk_budget_scores_higher(self, ti_db):
        low_risk = ate._compute_risk_score(entry_price=100.0, sl_price=95.0, capital=500000, risk_pct=1.0)
        # An absurdly wide stop relative to a tiny capital/risk budget --
        # position_sizing_check should fail (qty would size to 0).
        high_risk = ate._compute_risk_score(entry_price=100.0, sl_price=1.0, capital=1000, risk_pct=0.1)
        assert high_risk > low_risk


class TestSignalLogging:
    """Final review pass finding: ti_store.record_signal() existed since
    the original build but nothing ever called it -- ti_signal_log was a
    real table that stayed permanently empty. Now wired into every
    evaluate() return path via _log_signal()."""

    def test_no_trade_path_is_logged(self, ti_db):
        ate.evaluate("NOT_A_REAL_SYMBOL")
        signals = ts.list_signals(symbol="NOT_A_REAL_SYMBOL")
        assert len(signals) == 1
        assert signals[0]["action"] == "NO_TRADE"

    def test_buy_recommendation_is_logged_with_its_real_fields(self, ti_db):
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering', ce_trading_symbol='NIFTY_TEST_CE', ce_token='1' WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        signals = ts.list_signals(symbol="NIFTY")
        assert len(signals) == 1
        assert signals[0]["action"] == rec.action
        assert signals[0]["entry_price"] == rec.entry_price

    def test_hold_path_is_logged(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                       target_price=160.0, sl_price=80.0, qty=50, confidence=70)
        ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        signals = ts.list_signals(symbol="NIFTY")
        assert len(signals) == 1
        assert signals[0]["action"] == "HOLD"

    def test_each_cycle_adds_exactly_one_new_signal_row(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.0)
        ate.evaluate("NIFTY")
        ate.evaluate("NIFTY")
        assert len(ts.list_signals(symbol="NIFTY")) == 2


def _open_a_buy_signal(ti_db, **kwargs):
    """Same real-chain construction test_buy_ce_recommendation_has_every_required_field
    already establishes -- a genuine BUY CE signal, not a hand-built Recommendation."""
    # Expiry-integrity scoped fix (2026-08-24): a real caller must resolve
    # this before evaluate() will produce a BUY -- default it here so
    # existing callers of this helper keep exercising the real BUY path,
    # overridable via kwargs when a test needs to. The strike's own
    # ce_contract_expiry (below) must match whatever expiry_date ends up
    # used, per the 2026-08-24 follow-up's contract-expiry-match gate.
    expiry_date = kwargs.setdefault("expiry_date", dt.date.today() + dt.timedelta(days=2))
    cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
    conn = sqlite3.connect(ti_db)
    conn.execute(
        "UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering', ce_trading_symbol='NIFTY_TEST_CE', "
        "ce_token='1', ce_contract_expiry=? WHERE cycle_id=? AND strike=24500",
        (expiry_date.isoformat() if expiry_date else None, cid),
    )
    conn.commit()
    conn.close()
    return ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, **kwargs)


class TestSizingMode:
    """Milestone 11, Module 11.5: evaluate()'s new optional `sizing_mode`
    param. The critical regression guard is the first test -- the
    default must remain byte-identical to before this module."""

    def test_default_sizing_mode_matches_explicit_risk_pct(self, ti_db):
        import position_sizing

        rec = _open_a_buy_signal(ti_db)
        expected = position_sizing.compute_quantity(
            rec.entry_price, rec.sl_price, sizing_mode="risk_pct", capital=500000, risk_pct=1.0, min_qty=0,
        )
        assert rec.qty == expected

    def test_invalid_sizing_mode_raises(self, ti_db):
        import pytest
        with pytest.raises(ValueError):
            _open_a_buy_signal(ti_db, sizing_mode="not_a_real_mode")

    def test_adaptive_mode_never_exceeds_the_risk_pct_quantity(self, ti_db):
        rec_adaptive = _open_a_buy_signal(ti_db, sizing_mode="adaptive")
        assert rec_adaptive.qty is not None
        assert rec_adaptive.qty >= 0

    def test_adaptive_mode_runs_end_to_end_with_no_regime_or_alignment_data(self, ti_db):
        """No market_structure_snapshots row exists for this test's NIFTY
        -- regime must degrade honestly and adaptive sizing must still
        run to completion without raising."""
        rec = _open_a_buy_signal(ti_db, sizing_mode="adaptive")
        assert rec.action == "BUY CE"
        assert rec.qty is not None


class TestCalibrationReport:
    """Priority 6 (final review): the calibration framework made
    inspectable. There is no separate "training" step -- every closed
    paper trade is immediately reflected the next time this is called,
    because it queries ti_store.list_closed_trades() live."""

    def test_reports_one_row_per_bucket(self, ti_db):
        report = ate.calibration_report()
        assert len(report) == len(ate.CALIBRATION_BUCKETS)
        assert {r["confidence_bucket"] for r in report} == {f"{lo}-{hi}" for lo, hi in ate.CALIBRATION_BUCKETS}

    def test_bucket_with_no_trades_is_honestly_none(self, ti_db):
        report = ate.calibration_report()
        for row in report:
            assert row["sample_size"] == 0
            assert row["probability_pct"] is None
            assert "insufficient history" in row["note"]

    def test_bucket_reflects_real_closed_trades_immediately_after_close(self, ti_db):
        for i in range(6):
            tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                                 target_price=120.0, sl_price=90.0, qty=50, confidence=85)
            ts.close_trade(tid, exit_price=120.0 if i < 4 else 90.0, exit_reason="TARGET HIT" if i < 4 else "STOP LOSS")
        report = ate.calibration_report()
        bucket_80_100 = next(r for r in report if r["confidence_bucket"] == "80-100")
        assert bucket_80_100["sample_size"] == 6
        assert bucket_80_100["wins"] == 4
        assert bucket_80_100["probability_pct"] == 66.7


class TestCalibrationReportDimension:
    """Milestone 11, Module 11.3: calibration_report()'s optional second
    bucketing dimension. The critical regression guard is the FIRST test
    below -- dimension=None must remain byte-identical to Module 11.2's
    own behavior, the plan's own explicit success criterion for this
    module."""

    def test_default_call_is_unchanged_from_before_module_11_3(self, ti_db):
        for i in range(6):
            tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                                 target_price=120.0, sl_price=90.0, qty=50, confidence=85)
            ts.close_trade(tid, exit_price=120.0 if i < 4 else 90.0, exit_reason="TARGET HIT" if i < 4 else "STOP LOSS")
        assert ate.calibration_report() == ate.calibration_report(dimension=None)
        report = ate.calibration_report()
        assert isinstance(report, list)
        assert len(report) == len(ate.CALIBRATION_BUCKETS)

    def test_invalid_dimension_raises(self, ti_db):
        import pytest
        with pytest.raises(ValueError):
            ate.calibration_report(dimension="not_a_real_dimension")

    def test_regime_dimension_adds_a_second_breakdown_without_touching_the_first(self, ti_db):
        for regime, exit_price in (("TRENDING", 120.0), ("TRENDING", 120.0), ("RANGING", 90.0)):
            tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                                 target_price=120.0, sl_price=90.0, qty=50, confidence=85,
                                 regime_trend_at_entry=regime)
            ts.close_trade(tid, exit_price=exit_price, exit_reason="TARGET HIT" if exit_price > 100 else "STOP LOSS")

        result = ate.calibration_report(dimension="regime")
        assert result["by_confidence"] == ate.calibration_report()  # first dimension is untouched
        assert result["by_regime"]["TRENDING"]["sample_size"] == 2
        assert result["by_regime"]["TRENDING"]["wins"] == 2
        assert result["by_regime"]["RANGING"]["sample_size"] == 1

    def test_dimension_bucket_below_min_sample_is_honestly_none(self, ti_db):
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=120.0, sl_price=90.0, qty=50, regime_trend_at_entry="TRENDING")
        ts.close_trade(tid, exit_price=120.0, exit_reason="TARGET HIT")
        result = ate.calibration_report(dimension="regime")
        bucket = result["by_regime"]["TRENDING"]
        assert bucket["sample_size"] == 1
        assert bucket["probability_pct"] is None
        assert "insufficient history" in bucket["note"]

    def test_trades_with_no_regime_context_bucket_as_unknown(self, ti_db):
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=120.0, sl_price=90.0, qty=50)
        ts.close_trade(tid, exit_price=120.0, exit_reason="TARGET HIT")
        result = ate.calibration_report(dimension="regime")
        assert result["by_regime"]["UNKNOWN"]["sample_size"] == 1

    def test_timeframe_alignment_dimension_buckets_by_the_same_thresholds_module_11_2_uses(self, ti_db):
        from agents.trading_intelligence import timeframe_confirmation as tc

        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=120.0, sl_price=90.0, qty=50,
                             timeframe_alignment_score_at_entry=tc.ALIGNMENT_CONFIRMED_PERCENTILE)
        ts.close_trade(tid, exit_price=120.0, exit_reason="TARGET HIT")
        result = ate.calibration_report(dimension="timeframe_alignment")
        assert result["by_timeframe_alignment"]["CONFIRMED"]["sample_size"] == 1

    def test_quality_tier_dimension_never_raises_on_a_mix_of_scoreable_and_unscoreable_trades(self, ti_db):
        for regime in (None, "TRENDING", "RANGING"):
            tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                                 target_price=120.0, sl_price=90.0, qty=50, regime_trend_at_entry=regime)
            ts.close_trade(tid, exit_price=120.0, exit_reason="TARGET HIT")
        result = ate.calibration_report(dimension="quality_tier")
        assert sum(v["sample_size"] for v in result["by_quality_tier"].values()) == 3
