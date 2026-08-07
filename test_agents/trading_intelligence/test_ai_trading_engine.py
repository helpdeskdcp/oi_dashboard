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
        conn.execute("UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering' WHERE cycle_id=? AND strike=24500", (cid,))
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
