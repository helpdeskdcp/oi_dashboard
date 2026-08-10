"""
test_engine.py -- formal regression test suite for the core trading-logic
functions (sr_probability_engine.py, market_structure.py, oi_engine.py).

WHY THIS EXISTS: several real bugs were found tonight via manual ad-hoc
testing (a missing return-brace that silently deleted a function signature,
an SL-calculation bug that produced unrealistically tight stops, a JS syntax
error that broke the live dashboard). Manual testing catches bugs once;
these tests catch them FOREVER -- any future edit that reintroduces one of
these issues will fail a test immediately, not get discovered days later
during backtesting or (worse) live trading.

USAGE:
    pip install pytest --break-system-packages   (one-time)
    cd ~/oi_dashboard
    python3 -m pytest test_engine.py -v

Add new tests here whenever a new bug is found and fixed -- that's the
whole point of a regression suite.
"""
import datetime as dt
import time
import pytest

from oi_engine import StrikeRow, assess_market_quality, detect_bias, calc_pcr, oi_walls
from sr_probability_engine import (
    advance_level_state, check_structural_trigger, advance_active_level,
    compute_premium_entry_trigger, compute_premium_momentum, check_premium_momentum_confirmed,
    compute_dynamic_targets_sl, validate_risk_reward, score_strike_candidates,
    fake_breakout_filter, compute_volume_expansion, classify_price_structure,
    compute_institutional_entry_score, build_sr_probability_table,
)
from market_structure import detect_mother_candle, detect_liquidity_sweep, calc_atr


# ---------------------------------------------------------------------------
# compute_dynamic_targets_sl -- regression test for the tight-stop bug found
# via backtesting (R:R of 41x on a 0.5% stop was NOT genuine, it was a bug)
# ---------------------------------------------------------------------------
class TestStopLossClamping:
    """SL logic was redesigned 2026-07-21: the old 15%-35% flat clamp was
    replaced by a hard max_sl_pct cap (default 5%) combined with an adaptive
    swing-level structural stop, whichever is tighter (see
    compute_dynamic_targets_sl's docstring). These tests target that current
    design, not the retired 15%-35% one."""

    def test_no_swing_level_data_falls_back_to_flat_cap(self):
        """Without swing_level_underlying/underlying_price, SL must land
        exactly at the flat max_sl_pct cap (default 5%) -- ATR no longer
        factors into SL at all under the current design."""
        t1, t2, sl = compute_dynamic_targets_sl(
            entry_price=9.9, level_price=280, next_level_price=282,
            underlying_atr=0.3, delta_approx=0.55,
        )
        distance_pct = (9.9 - sl) / 9.9
        assert distance_pct == pytest.approx(0.05, abs=0.002), f"expected flat 5% cap, got {distance_pct:.1%}"

    def test_wide_swing_level_still_capped_at_max_sl_pct(self):
        """A far-away structural swing point must still be capped at the flat
        max_sl_pct (default 5%) -- the cap always wins when the structural
        stop would be wider (more risk) than it."""
        t1, t2, sl = compute_dynamic_targets_sl(
            entry_price=100, level_price=24350, next_level_price=24450,
            underlying_atr=500, delta_approx=0.55,
            swing_level_underlying=24000, underlying_price=24350,   # far swing point -> would be a wide stop uncapped
        )
        distance_pct = (100 - sl) / 100
        assert distance_pct == pytest.approx(0.05, abs=0.002), f"expected flat 5% cap, got {distance_pct:.1%}"

    def test_none_entry_price_returns_none_safely(self):
        result = compute_dynamic_targets_sl(None, 100, 110, 5)
        assert result == (None, None, None)

    def test_missing_structural_data_falls_back_to_flat_percentages(self):
        t1, t2, sl = compute_dynamic_targets_sl(entry_price=100, level_price=None,
                                                  next_level_price=None, underlying_atr=None)
        assert t1 == 115.0   # entry * 1.15 (min_target_pct=0.15 default)
        assert sl == 95.0    # entry * (1 - 0.05) (max_sl_pct=0.05 default, 2026-07-21 redesign)


# ---------------------------------------------------------------------------
# Entry trigger -- regression test for the self-referential ordering bug
# ---------------------------------------------------------------------------
class TestPremiumEntryTrigger:
    def test_trigger_is_none_with_insufficient_history(self):
        assert compute_premium_entry_trigger([{"ltp": 100}]) is None
        assert compute_premium_entry_trigger([]) is None

    def test_trigger_never_includes_the_evaluated_reading_itself(self):
        """Regression test: trigger must be computed PURELY from prior
        history, never including the current reading -- otherwise it becomes
        self-referential and can never be satisfied (the original bug)."""
        history = [{"ltp": 100}, {"ltp": 105}]
        trigger = compute_premium_entry_trigger(history)
        # trigger should be based on max(100,105)=105 plus a buffer, NOT
        # dependent on some external "current" value
        assert trigger == pytest.approx(105 * 1.005, rel=0.01)

    def test_genuine_breakout_of_recent_high_is_detected(self):
        history = [{"ltp": p} for p in [95, 97, 99, 98, 101, 103, 105, 107, 110, 113]]
        trigger = compute_premium_entry_trigger(history)
        assert 113 < trigger < 116   # small buffer above the recent high


# ---------------------------------------------------------------------------
# EMA momentum confirmation -- regression test for fakeout filtering
# ---------------------------------------------------------------------------
class TestPremiumMomentum:
    def test_genuine_rising_trend_confirms(self):
        rising = [{"ltp": p} for p in [95, 96, 98, 97, 99, 101, 103, 102, 105, 108, 110, 112]]
        confirmed, fast, slow = check_premium_momentum_confirmed(rising, fast_period=5, slow_period=10)
        assert confirmed is True
        assert fast > slow

    def test_one_tick_spike_in_downtrend_does_not_confirm(self):
        """A single tick spiking up within an overall declining trend must
        NOT be confirmed -- this is exactly the fakeout scenario the EMA
        gate exists to filter out."""
        declining_then_spike = [{"ltp": p} for p in [110, 108, 106, 104, 102, 100, 98, 96, 94, 92, 115]]
        confirmed, fast, slow = check_premium_momentum_confirmed(declining_then_spike, fast_period=5, slow_period=10)
        assert confirmed is False

    def test_insufficient_history_returns_none_not_false(self):
        confirmed, fast, slow = check_premium_momentum_confirmed([{"ltp": 100}], fast_period=5, slow_period=10)
        assert confirmed is None   # not enough data -- must not be treated as a hard rejection


# ---------------------------------------------------------------------------
# Fake Breakout Filter
# ---------------------------------------------------------------------------
class TestFakeBreakoutFilter:
    def test_all_checks_passing_allows_trade(self):
        passes, failed = fake_breakout_filter(volume_expanded=True, oi_supports_direction=True,
                                                premium_rising=True, vwap_aligned=True)
        assert passes is True
        assert failed == []

    def test_weak_volume_alone_blocks_trade(self):
        passes, failed = fake_breakout_filter(volume_expanded=False, oi_supports_direction=True,
                                                premium_rising=True, vwap_aligned=True)
        assert passes is False
        assert len(failed) == 1

    def test_missing_data_does_not_block_trade(self):
        """None (unknown) should not be treated as a rejection -- only
        confirmed-false data should block."""
        passes, failed = fake_breakout_filter(volume_expanded=None, oi_supports_direction=True,
                                                premium_rising=True, vwap_aligned=True)
        assert passes is True

    def test_volume_expansion_detects_genuine_spike(self):
        hist = [10000, 12000, 11000, 9000, 10500]
        expanded, ratio = compute_volume_expansion(hist, current_volume=25000)
        assert expanded is True
        assert ratio > 2.0

    def test_volume_expansion_rejects_thin_breakout(self):
        hist = [10000, 12000, 11000, 9000, 10500]
        expanded, ratio = compute_volume_expansion(hist, current_volume=8000)
        assert expanded is False


# ---------------------------------------------------------------------------
# State machine progression + proximity gate
# ---------------------------------------------------------------------------
class TestStateMachine:
    def test_progresses_through_states_with_persistence(self):
        level_eval = {"breakout_probability": 100, "reversal_probability": 0}
        st = None
        st = advance_level_state(level_eval, st, persistence_seconds=1)
        time.sleep(1.1)
        st = advance_level_state(level_eval, st, persistence_seconds=1)
        assert st["state"] in ("WATCH", "ARMED", "CONFIRMED")   # progressed past NO_EDGE

    def test_far_from_price_caps_at_armed_not_confirmed(self):
        """Regression test: a level with 100% probability but far from
        current price must NOT reach CONFIRMED (proximity gate)."""
        level_eval = {"breakout_probability": 100, "reversal_probability": 0}
        st = None
        st = advance_level_state(level_eval, st, persistence_seconds=1,
                                  underlying=77460, level_price=77845.5, proximity_atr=79, proximity_mult=3.0)
        time.sleep(1.1)
        st = advance_level_state(level_eval, st, persistence_seconds=1,
                                  underlying=77460, level_price=77845.5, proximity_atr=79, proximity_mult=3.0)
        assert st["state"] != "CONFIRMED"
        assert "too far" in (st.get("distance_label") or "")

    def test_close_to_price_can_reach_confirmed(self):
        level_eval = {"breakout_probability": 100, "reversal_probability": 0}
        st = None
        st = advance_level_state(level_eval, st, persistence_seconds=1,
                                  underlying=77800, level_price=77845.5, proximity_atr=79, proximity_mult=3.0)
        time.sleep(1.1)
        st = advance_level_state(level_eval, st, persistence_seconds=1,
                                  underlying=77800, level_price=77845.5, proximity_atr=79, proximity_mult=3.0)
        assert st["state"] == "CONFIRMED"


# ---------------------------------------------------------------------------
# Structural trigger -- wick vs close confirmation
# ---------------------------------------------------------------------------
class TestStructuralTrigger:
    def test_genuine_breakout_triggers(self):
        assert check_structural_trigger("resistance", "breakout", 24350, 24360, tolerance=10) is True

    def test_price_not_through_tolerance_does_not_trigger(self):
        assert check_structural_trigger("resistance", "breakout", 24350, 24352, tolerance=10) is False


# ---------------------------------------------------------------------------
# Mother Candle + Inside Bar detection
# ---------------------------------------------------------------------------
class TestMotherCandle:
    def _baseline_candles(self, n=20, vol=1000):
        return [{"open": 100, "high": 101, "low": 99, "close": 100.5, "volume": vol} for _ in range(n)]

    def test_confirmed_close_breakout_detected(self):
        candles = self._baseline_candles()
        candles.append({"open": 100, "high": 110, "low": 98, "close": 109, "volume": 5000})  # mother
        candles.append({"open": 105, "high": 108, "low": 102, "close": 106, "volume": 800})   # inside
        candles.append({"open": 106, "high": 112, "low": 105, "close": 111, "volume": 3000})  # confirmed breakout
        result = detect_mother_candle(candles, atr=5.0)
        assert result["found"] is True
        assert result["breakout_confirmed"] is True
        assert result["breakout_direction"] == "bullish"

    def test_wick_only_poke_is_not_confirmed(self):
        """Regression test: a wick that pokes outside the range but CLOSES
        back inside must NOT count as a confirmed breakout."""
        candles = self._baseline_candles()
        candles.append({"open": 100, "high": 110, "low": 98, "close": 109, "volume": 5000})
        candles.append({"open": 106, "high": 111, "low": 104, "close": 105, "volume": 1200})  # wick above, closes inside
        result = detect_mother_candle(candles, atr=5.0)
        assert result["breakout_confirmed"] is False
        assert result["breakout_direction"] is None


# ---------------------------------------------------------------------------
# Liquidity Sweep + Reclaim
# ---------------------------------------------------------------------------
class TestLiquiditySweep:
    def test_bullish_sweep_and_reclaim_detected(self):
        candles = [
            {"open": 24050, "high": 24060, "low": 24040, "close": 24045, "volume": 1000},
            {"open": 24045, "high": 24048, "low": 23980, "close": 24010, "volume": 3000},  # sweeps below PDL then reclaims
        ]
        result = detect_liquidity_sweep(candles, pdh=24200, pdl=24000)
        assert result["swept"] == "bullish"
        assert result["reclaimed"] is True

    def test_no_sweep_when_price_stays_in_range(self):
        candles = [{"open": 24050, "high": 24060, "low": 24040, "close": 24045, "volume": 1000}]
        result = detect_liquidity_sweep(candles, pdh=24200, pdl=24000)
        assert result["swept"] is None


# ---------------------------------------------------------------------------
# Institutional Entry Score
# ---------------------------------------------------------------------------
class TestInstitutionalScore:
    def test_strong_setup_scores_high(self):
        result = compute_institutional_entry_score(
            price_structure="HIGHER_HIGH_HIGHER_LOW", oi_evidence_pct=85, vwap_aligned=True,
            regime="TRENDING", premium_momentum_confirmed=True, wall_cross_verified=True,
            liquidity_score=0.8, risk_reward_ok=True,
        )
        assert result["score"] >= 90
        assert result["tier"] in ("VERY HIGH QUALITY", "EXCEPTIONAL SETUP")

    def test_weak_setup_scores_low(self):
        result = compute_institutional_entry_score(
            price_structure="MIXED", oi_evidence_pct=55, vwap_aligned=False,
            regime="TRANSITIONING", premium_momentum_confirmed=False, wall_cross_verified=False,
            liquidity_score=0.3, risk_reward_ok=False,
        )
        assert result["score"] < 70
        assert result["tier"] == "NO TRADE"


# ---------------------------------------------------------------------------
# R:R validator
# ---------------------------------------------------------------------------
class TestRiskReward:
    def test_good_rr_passes(self):
        rr, ok = validate_risk_reward(entry_price=100, target1=150, sl=80, min_rr=1.5)
        assert rr == 2.5
        assert ok is True

    def test_poor_rr_fails(self):
        rr, ok = validate_risk_reward(entry_price=100, target1=110, sl=80, min_rr=1.5)
        assert ok is False

    def test_zero_or_negative_risk_is_safely_rejected(self):
        rr, ok = validate_risk_reward(entry_price=100, target1=110, sl=100, min_rr=1.5)
        assert ok is False


# ---------------------------------------------------------------------------
# Strike scoring / liquidity rejection
# ---------------------------------------------------------------------------
class TestStrikeScoring:
    def test_illiquid_strike_scores_zero(self):
        rows = [StrikeRow(strike=24150, ce_oi=0, ce_oi_chg=0, ce_vol=0, ce_ltp=0)]
        scored = score_strike_candidates(rows, atm=24150, strike_step=50, direction="CE")
        assert scored[0]["liquidity_ok"] is False
        assert scored[0]["score"] == 0.0

    def test_liquid_strike_scores_positive(self):
        rows = [StrikeRow(strike=24200, ce_oi=200000, ce_oi_chg=5000, ce_vol=50000, ce_ltp=105)]
        scored = score_strike_candidates(rows, atm=24200, strike_step=50, direction="CE")
        assert scored[0]["liquidity_ok"] is True
        assert scored[0]["score"] > 0


# ---------------------------------------------------------------------------
# ATR sanity
# ---------------------------------------------------------------------------
class TestATR:
    def test_atr_is_positive_for_normal_candles(self):
        candles = [{"high": 100 + i, "low": 95 + i, "close": 98 + i} for i in range(20)]
        atr = calc_atr(candles, period=14)
        assert atr is not None
        assert atr > 0

    def test_atr_none_with_insufficient_candles(self):
        candles = [{"high": 100, "low": 95, "close": 98}]
        assert calc_atr(candles, period=14) is None


# ---------------------------------------------------------------------------
# Milestone 14 observability pass: assess_market_quality()
# ---------------------------------------------------------------------------
class TestMarketQuality:
    def test_all_zero_oi_and_volume_is_no_liquidity(self):
        rows = [
            StrikeRow(strike=152500, ce_oi=0, ce_vol=0, pe_oi=0, pe_vol=0),
            StrikeRow(strike=152600, ce_oi=0, ce_vol=0, pe_oi=0, pe_vol=0),
        ]
        assert assess_market_quality(rows) == "NO_LIQUIDITY"

    def test_low_oi_or_low_volume_is_thin(self):
        rows = [StrikeRow(strike=152500, ce_oi=500, ce_vol=50, pe_oi=0, pe_vol=0)]
        assert assess_market_quality(rows) == "THIN"

    def test_high_oi_but_low_volume_is_still_thin(self):
        """Both floors must clear -- total_oi >= 1000 AND total_vol >= 100,
        matching the spec's `if total_oi < 1000 or total_vol < 100`."""
        rows = [StrikeRow(strike=152500, ce_oi=5000, ce_vol=10, pe_oi=0, pe_vol=0)]
        assert assess_market_quality(rows) == "THIN"

    def test_real_activity_is_normal(self):
        rows = [
            StrikeRow(strike=24500, ce_oi=200000, ce_vol=50000, pe_oi=180000, pe_vol=45000),
            StrikeRow(strike=24550, ce_oi=150000, ce_vol=30000, pe_oi=140000, pe_vol=28000),
        ]
        assert assess_market_quality(rows) == "NORMAL"

    def test_empty_strikes_list_is_no_liquidity(self):
        assert assess_market_quality([]) == "NO_LIQUIDITY"


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-v"]))
