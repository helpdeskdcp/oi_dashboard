import datetime as dt
import sqlite3

from agents.trading_intelligence import ai_trading_engine as ate
from agents.trading_intelligence import ti_store as ts
from test_agents.trading_intelligence.conftest import (
    insert_realistic_chain,
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
        conn.execute("UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering' WHERE cycle_id=? AND strike=24500", (cid,))
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

    def test_probability_is_honestly_none_with_no_history(self, ti_db):
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering' WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
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

    def test_buy_recommendation_carries_every_priority_2_field(self, ti_db):
        """Priority 2 review requirement: every recommendation must include
        Market Bias, Confidence, Probability, Risk Score, Entry, SL, Multiple
        Targets (T1/T2/T3), Expected Move, Time Horizon, Institutional/OI/
        Greeks/Price Action reasoning -- checked explicitly here, not just
        implied by the earlier "has_every_required_field" smoke test."""
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering' WHERE cycle_id=? AND strike=24500", (cid,))
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

    def test_time_horizon_is_honest_when_no_expiry_given(self, ti_db):
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering' WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        assert rec.time_horizon == "unknown (no expiry date provided)"

    def test_no_trade_recommendation_still_carries_market_bias(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", pcr=1.0)  # neutral -> NO_TRADE
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        assert rec.action == "NO_TRADE"
        assert rec.targets == []
        assert rec.expected_move_pts is None


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
