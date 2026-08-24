"""
test_agents/trading_intelligence/test_validation.py -- Milestone 10 review,
Priority 5: Replay / Stress / Performance / Memory / Trading validation.
Extended for Milestone 11 Phase 7 (Full Validation & Documentation) with a
cross-module integration suite exercising Modules 11.1-11.6 together,
matching this file's own established convention rather than a new one.

Every test here still goes through the ti_db fixture (a real-shaped,
throwaway SQLite schema) -- never a live broker call, same as every other
file in this package (see test_safety.py's own AST scan). The point of
this file specifically is END-TO-END behavior under realistic *shape* and
*volume*, not unit-level correctness (already covered elsewhere).
"""
import datetime as dt
import os
import sqlite3
import time

from agents.trading_intelligence import adaptive_sizing as ads
from agents.trading_intelligence import ai_trading_engine as ate
from agents.trading_intelligence import api as ti_api
from agents.trading_intelligence import explainability as ex
from agents.trading_intelligence import paper_trading as pt
from agents.trading_intelligence import strike_intelligence as si
from agents.trading_intelligence import ti_store as ts
from agents.trading_intelligence import trade_quality as tq
from test_agents.trading_intelligence.conftest import (
    insert_cycle, insert_market_structure, insert_realistic_chain, insert_strike,
)


class TestReplay:
    """Feed the engine a real multi-cycle sequence -- rising IV, shifting
    OI buildup, a drifting underlying -- and assert every single cycle
    produces internally consistent output. This is what actually happens
    across a trading session: the SAME symbol re-evaluated cycle after
    cycle, not one isolated snapshot."""

    def test_20_cycle_replay_never_raises_and_stays_internally_consistent(self, ti_db):
        underlying = 24500.0
        for i in range(20):
            underlying += (5 if i % 2 == 0 else -3)  # a real, small, oscillating drift
            cid, strikes = insert_realistic_chain(
                ti_db, symbol="NIFTY", underlying_ltp=underlying, atm=round(underlying / 50) * 50,
                pcr=1.0 + (i % 5) * 0.1, ts=f"2026-08-06T09:{15 + i}:00",
            )
            overview = ti_api.get_symbol_overview("NIFTY")
            assert overview["available"] is True
            assert overview["market_data"]["underlying_ltp"] == underlying
            rec = overview["recommendation"]
            assert rec["action"] in ("BUY CE", "BUY PE", "HOLD", "NO_TRADE")
            # A recommendation must never claim a target/SL without an entry,
            # or vice versa -- the same internal-consistency invariant a
            # dashboard renderer would rely on.
            if rec["action"] in ("BUY CE", "BUY PE"):
                assert rec["entry_price"] is not None and rec["sl_price"] is not None
                assert rec["targets"] and rec["targets"][0] == rec["target_price"]
            for strike_row in overview["strikes"]:
                assert 0 <= strike_row["ai_strike_score"] <= 100


class TestStress:
    """A wide chain (25 strikes) and a long per-strike history -- shapes
    the original 5-9 strike unit tests never exercise -- checked for
    actual correctness, not just "did not crash.\""""

    def test_wide_chain_25_strikes_builds_a_correct_table(self, ti_db):
        cid, strikes = insert_realistic_chain(
            ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, strikes_each_side=12,
        )
        overview = ti_api.get_symbol_overview("NIFTY", expiry_date=__import__("datetime").date.today()
                                               + __import__("datetime").timedelta(days=3))
        assert len(overview["strikes"]) == 25
        assert sum(1 for s in overview["strikes"] if s["is_max_pain"]) == 1
        oi_wall_total_share = sum(s["oi_wall_score"] for s in overview["strikes"])
        assert 99.0 <= oi_wall_total_share <= 101.0  # shares of one chain must sum to ~100%

    def test_long_history_iv_rank_and_premium_momentum_stay_bounded(self, ti_db):
        strike = 24500
        for i in range(60):
            cid = insert_cycle(ti_db, symbol="NIFTY", ts=f"2026-08-06T{9 + i // 60:02d}:{i % 60:02d}:00")
            insert_strike(ti_db, cid, strike, ce_iv=14.0 + (i % 10) * 0.3, ce_ltp=100.0 + (i % 20))
        from oi_engine import StrikeRow
        rows = [StrikeRow(strike=strike, ce_oi=80000, ce_iv=16.0, ce_ltp=115.0, pe_oi=60000, pe_iv=15.0, pe_ltp=90.0)]
        table = si.build_table("NIFTY", rows, underlying=24505)
        atm = table[0]
        assert atm.ce_iv_rank is not None and 0 <= atm.ce_iv_rank <= 100
        assert atm.ce_premium_momentum is not None


class TestPerformance:
    """Soft wall-clock budgets on the two hottest per-cycle paths. Generous
    on purpose (this is SQLite-on-tmpfs, not production Postgres) -- the
    point is to catch a genuine N^2-shaped regression (e.g. a re-fetch per
    strike), not to micro-benchmark."""

    def test_get_symbol_overview_completes_quickly_for_a_realistic_chain(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, strikes_each_side=9)
        import datetime as dt
        start = time.perf_counter()
        for _ in range(5):
            ti_api.get_symbol_overview("NIFTY", expiry_date=dt.date.today() + dt.timedelta(days=2))
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"5 calls to get_symbol_overview took {elapsed:.2f}s -- investigate a regression"

    def test_build_table_scales_reasonably_with_strike_count(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, strikes_each_side=12)
        cycle = ti_api.get_symbol_overview("NIFTY")
        from agents.trading_intelligence import market_data
        snapshot = market_data.get_snapshot("NIFTY")
        start = time.perf_counter()
        si.build_table("NIFTY", snapshot.strikes, underlying=snapshot.underlying_ltp)
        elapsed = time.perf_counter() - start
        assert elapsed < 2.0, f"build_table on 25 strikes took {elapsed:.2f}s -- investigate a regression"


class TestMemory:
    """No SQLite file-descriptor leak across many repeated calls. Every
    agents.trading_intelligence.data_access function opens and closes its
    own connection per call (by design, per that module's own docstring) --
    this verifies that contract actually holds under load, not just by
    reading the source."""

    def test_repeated_overview_calls_do_not_leak_file_descriptors(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        fd_dir = "/proc/self/fd"
        if not os.path.isdir(fd_dir):
            import pytest
            pytest.skip("no /proc/self/fd on this platform")

        for _ in range(10):  # warm up any one-time caches/imports
            ti_api.get_symbol_overview("NIFTY")
        baseline = len(os.listdir(fd_dir))

        for _ in range(50):
            ti_api.get_symbol_overview("NIFTY")

        after = len(os.listdir(fd_dir))
        assert after - baseline <= 2, (
            f"open file descriptors grew from {baseline} to {after} across 50 calls -- "
            "a connection is probably not being closed"
        )


class TestTradingValidation:
    """A full paper-trade lifecycle driven end-to-end exactly the way a
    live scheduler cycle would: evaluate() produces a BUY, the trade is
    opened, the market moves to hit the target, the NEXT evaluate() call
    auto-closes it, and performance_stats() reflects the closed trade --
    never asserting on any one function in isolation."""

    def test_full_buy_to_target_hit_lifecycle_reflected_in_performance_stats(self, ti_db):
        import sqlite3

        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
        conn = sqlite3.connect(ti_db)
        conn.execute(
            "UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering' WHERE cycle_id=? AND strike=24500",
            (cid,),
        )
        conn.commit()
        conn.close()

        # Expiry-integrity scoped fix (2026-08-24): evaluate() now fails
        # closed on an actionable BUY without a resolved expiry_date.
        rec = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, expiry_date=dt.date.today() + dt.timedelta(days=2))
        assert rec.action in ("BUY CE", "BUY PE")
        tid = pt.enter_from_recommendation(rec)
        assert tid is not None
        assert len(ts.list_open_trades(symbol="NIFTY")) == 1

        # Move the underlying's stored LTP for this strike past the target
        # (a new cycle, the same way a live scheduler tick would look).
        direction_ltp_col = "ce_ltp" if rec.direction == "CE" else "pe_ltp"
        conn = sqlite3.connect(ti_db)
        conn.execute(
            f"UPDATE strikes SET {direction_ltp_col}=? WHERE cycle_id=? AND strike=?",
            (rec.target_price + 5, cid, rec.strike),
        )
        conn.commit()
        conn.close()

        rec2 = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        assert rec2.action == "NO_TRADE"
        assert "TARGET HIT" in rec2.reasoning
        assert ts.list_open_trades(symbol="NIFTY") == []

        stats = pt.performance_stats(symbol="NIFTY")
        assert stats["total_trades"] == 1
        assert stats["wins"] == 1


def _open_a_real_buy_signal(ti_db, **evaluate_kwargs):
    """Same real-chain construction TestTradingValidation's own lifecycle
    test already establishes -- a genuine BUY CE signal, not a hand-built
    Recommendation, so every downstream M11 module reads real evidence."""
    cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500, pcr=1.35)
    conn = sqlite3.connect(ti_db)
    conn.execute(
        "UPDATE strikes SET ce_oi_chg=-2000, ce_signal='Short Covering' WHERE cycle_id=? AND strike=24500", (cid,),
    )
    conn.commit()
    conn.close()
    # Expiry-integrity scoped fix (2026-08-24): a real caller must resolve
    # this before evaluate() will produce a BUY -- default it here so
    # existing callers of this helper keep exercising the real BUY path,
    # overridable via evaluate_kwargs when a test needs to.
    evaluate_kwargs.setdefault("expiry_date", dt.date.today() + dt.timedelta(days=2))
    return ate.evaluate("NIFTY", capital=500000, risk_pct=1.0, **evaluate_kwargs), cid


class TestMilestone11Integration:
    """Milestone 11, Phase 7 (Full Validation & Documentation): a single
    real lifecycle exercising Modules 11.1-11.6 TOGETHER -- regime
    (11.1), timeframe confirmation (11.2), trade quality (11.3),
    explainability (11.4), adaptive sizing (11.5), and the Sortino/equity
    extension (11.6) -- never asserting on any one module in isolation,
    the same discipline TestTradingValidation already established for
    the Milestone 10 pipeline."""

    def test_full_pipeline_from_adaptive_recommendation_to_scored_and_explained_closed_trade(self, ti_db):
        insert_market_structure(ti_db, symbol="NIFTY", ts="2026-08-06T10:00:00", adx=30.0, atr_14=15.0)

        # 11.1 + 11.2 + 11.5: a real BUY CE, sized adaptively (regime +
        # timeframe + institutional evidence feed the quantity).
        rec, cid = _open_a_real_buy_signal(ti_db, sizing_mode="adaptive")
        assert rec.action == "BUY CE"
        assert rec.qty is not None and rec.qty >= 0

        adaptive = ads.compute_adaptive_quantity(
            rec.entry_price, rec.sl_price, capital=500000, risk_pct=1.0, symbol="NIFTY",
        )
        assert rec.qty <= adaptive.base_qty  # the same bounded-by-risk_pct guarantee, reconfirmed in the full pipeline

        # 11.4: a live recommendation is explainable, citing real regime/
        # timeframe evidence, never a placeholder string.
        explanation = ex.explain_recommendation(rec)
        assert explanation.summary
        assert "confidence" in explanation.inputs_used

        # 11.3: enter_from_recommendation() captures regime/timeframe/
        # institutional context AT ENTRY (Module 11.3's own wiring).
        tid = pt.enter_from_recommendation(rec)
        assert tid is not None
        open_trade = ts.list_open_trades(symbol="NIFTY")[0]
        assert open_trade["regime_trend_at_entry"] == "TRENDING"

        # Move price to hit target, close the trade (a real win).
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_ltp=? WHERE cycle_id=? AND strike=?",
                     (rec.target_price + 5, cid, rec.strike))
        conn.commit()
        conn.close()
        rec2 = ate.evaluate("NIFTY", capital=500000, risk_pct=1.0)
        assert rec2.action == "NO_TRADE"
        closed_trade = ts.list_closed_trades(symbol="NIFTY")[0]
        assert closed_trade["status"] == "CLOSED"

        # 11.3: the closed trade now has a real, available Trade Quality Score.
        quality = tq.score(closed_trade)
        assert quality.available is True
        assert 0.0 <= quality.score <= 100.0

        # 11.4: that score is explainable too, citing the SAME components
        # trade_quality.score() itself computed -- never a re-derivation.
        quality_explanation = ex.explain_trade_quality(closed_trade)
        assert "Trade Quality Score:" in quality_explanation.summary
        assert "regime" in quality_explanation.inputs_used

        # 11.3: calibration_report()'s new dimensions see this trade,
        # WITHOUT changing the original confidence-only view.
        default_report = ate.calibration_report()
        for dimension in ("regime", "timeframe_alignment", "quality_tier"):
            dimensioned = ate.calibration_report(dimension=dimension)
            assert dimensioned["by_confidence"] == default_report
            assert dimensioned[f"by_{dimension}"]  # at least one real bucket populated

        # 11.6: performance_stats() reflects the closed trade with the
        # new Sortino/equity-curve fields alongside every pre-existing one.
        stats = pt.performance_stats(symbol="NIFTY")
        assert stats["total_trades"] == 1
        assert stats["wins"] == 1
        assert stats["equity_curve"] == [stats["net_pnl"]]


class TestMilestone11Replay:
    """The plan's own established Replay discipline (TestReplay above),
    extended across a real multi-cycle sequence with sizing_mode="adaptive"
    engaged the whole time -- every M11.1-11.5 read (regime, timeframe,
    institutional backing, streak dampener) must survive real cycle-over-
    cycle state accumulation without raising, exactly the volume a live
    scheduler would produce."""

    def test_15_cycle_adaptive_replay_never_raises_and_stays_internally_consistent(self, ti_db):
        underlying = 24500.0
        for i in range(15):
            underlying += (5 if i % 2 == 0 else -3)
            insert_realistic_chain(
                ti_db, symbol="NIFTY", underlying_ltp=underlying, atm=round(underlying / 50) * 50,
                pcr=1.0 + (i % 5) * 0.1, ts=f"2026-08-06T09:{15 + i}:00",
            )
            insert_market_structure(
                ti_db, symbol="NIFTY", ts=f"2026-08-06T09:{15 + i}:00",
                adx=10.0 + (i % 20), atr_14=8.0 + (i % 7),
            )
            rec = ate.evaluate(
                "NIFTY", capital=500000, risk_pct=1.0, sizing_mode="adaptive",
                expiry_date=dt.date.today() + dt.timedelta(days=2),
            )
            assert rec.action in ("BUY CE", "BUY PE", "HOLD", "NO_TRADE")
            if rec.action in ("BUY CE", "BUY PE"):
                assert rec.qty is not None and rec.qty >= 0
                tid = pt.enter_from_recommendation(rec)
                if tid is not None:
                    trade = [t for t in ts.list_open_trades(symbol="NIFTY") if t["id"] == tid][0]
                    assert trade["regime_trend_at_entry"] in ("TRENDING", "RANGING", "TRANSITIONING", "UNKNOWN")


class TestMilestone11Performance:
    """Soft wall-clock budget on the new hottest per-cycle path: a BUY
    signal sized adaptively (three extra reads over plain risk_pct
    sizing: regime, timeframe alignment, institutional backing, plus
    adaptive_sizing's own track-record and streak-dampener queries)."""

    def test_adaptive_sizing_mode_completes_quickly_for_a_realistic_signal(self, ti_db):
        insert_market_structure(ti_db, symbol="NIFTY", ts="2026-08-06T10:00:00", adx=30.0)
        start = time.perf_counter()
        for _ in range(5):
            _open_a_real_buy_signal(ti_db, sizing_mode="adaptive")
        elapsed = time.perf_counter() - start
        assert elapsed < 5.0, f"5 adaptive-mode evaluate() calls took {elapsed:.2f}s -- investigate a regression"


class TestMilestone11Memory:
    """No SQLite file-descriptor leak across many repeated adaptive-mode
    evaluate() calls -- the same contract TestMemory already verifies for
    the plain M10 pipeline, re-checked for the new M11.1/11.2/11.5 reads
    each adaptive evaluation now performs."""

    def test_repeated_adaptive_evaluate_calls_do_not_leak_file_descriptors(self, ti_db):
        insert_market_structure(ti_db, symbol="NIFTY", ts="2026-08-06T10:00:00", adx=30.0)
        fd_dir = "/proc/self/fd"
        if not os.path.isdir(fd_dir):
            import pytest
            pytest.skip("no /proc/self/fd on this platform")

        for _ in range(5):
            _open_a_real_buy_signal(ti_db, sizing_mode="adaptive")
        baseline = len(os.listdir(fd_dir))

        for _ in range(20):
            _open_a_real_buy_signal(ti_db, sizing_mode="adaptive")

        after = len(os.listdir(fd_dir))
        assert after - baseline <= 2, (
            f"open file descriptors grew from {baseline} to {after} across 20 adaptive-mode calls -- "
            "a connection is probably not being closed"
        )
