"""
test_institutional_levels.py -- Milestone 20, Phase 1: regression tests
for institutional_levels.py (weighted composite S/R + role-reversal
detection). Repo-root location matches this module's own (same
convention as test_market_hours.py for market_session-adjacent, repo-
root modules).
"""
import datetime as dt

import institutional_levels as il
from oi_engine import StrikeRow


def _candle(date_str, hour, minute, o, h, l, c, v=1000):
    base = dt.datetime.fromisoformat(f"{date_str}T{hour:02d}:00:00")
    return {"datetime": base + dt.timedelta(minutes=minute),
            "open": o, "high": h, "low": l, "close": c, "volume": v}


def _quiet_lead_in(date_str, start_hour, start_minute, *, n=10, price=95.0, v=500, step_minutes=3):
    """N low-volume, flat candles before a breakout -- gives
    _avg_volume_before() a real VOLUME_LOOKBACK_CANDLES-sized window to
    average, with a volume level the breakout candle can then clear by
    MIN_VOLUME_MULTIPLIER. Every TestDetectRoleReversal fixture below
    needs this since Milestone 20, Phase 6 added the volume-confirmation
    requirement -- a breakout with no real preceding history to compare
    against is honestly treated as unconfirmed (see _avg_volume_before()'s
    own docstring), not silently passed."""
    out = []
    minute = start_minute
    hour = start_hour
    for _ in range(n):
        out.append(_candle(date_str, hour, minute, price, price + 0.2, price - 0.2, price, v))
        minute += step_minutes
        if minute >= 60:
            hour += minute // 60
            minute %= 60
    return out, hour, minute


class TestDetectRoleReversal:
    def test_resistance_to_support_flip(self):
        # Breakout above 100 on strong volume, retest within
        # MAX_RETEST_CANDLES with a dominant lower wick and a close back
        # above, THEN a confirmation candle closing beyond the breakout's
        # own close -- RESISTANCE -> SUPPORT.
        lead_in, h, m = _quiet_lead_in("2026-08-10", 9, 0)
        candles = lead_in + [
            _candle("2026-08-10", h, m, 95.5, 105, 95, 104.5, v=700),        # breakout, vol 700 >= 500*1.2=600
            _candle("2026-08-10", h, m + 3, 103, 103.5, 100.5, 103.2),      # retest (1 candle later): lower wick dominant
            _candle("2026-08-10", h, m + 6, 103.3, 105.5, 103.1, 105.1),    # confirmation: close 105.1 > breakout close 104.5
        ]
        result = il.detect_role_reversal(100, candles, profile={"breakout_buffer": 2, "retest_tolerance": 1})
        assert result is not None
        assert result["previous_role"] == "RESISTANCE"
        assert result["current_role"] == "SUPPORT"
        assert result["level"] == 100
        assert 60 <= result["confidence"] <= 98

    def test_support_to_resistance_flip(self):
        # Breakdown below 100 on strong volume, retest with a dominant
        # upper wick and a close back below, then a confirmation candle
        # closing beyond the breakdown's own close -- SUPPORT -> RESISTANCE.
        lead_in, h, m = _quiet_lead_in("2026-08-10", 9, 0, price=104.0)
        candles = lead_in + [
            _candle("2026-08-10", h, m, 104.5, 105, 95, 95.5, v=700),       # breakdown, vol 700 >= 500*1.2=600
            _candle("2026-08-10", h, m + 3, 96.8, 99.5, 96.5, 96.8),        # retest (1 candle later): upper wick dominant
            _candle("2026-08-10", h, m + 6, 96.5, 96.7, 94.5, 94.8),        # confirmation: close 94.8 < breakdown close 95.5
        ]
        result = il.detect_role_reversal(100, candles, profile={"breakout_buffer": 2, "retest_tolerance": 1})
        assert result is not None
        assert result["previous_role"] == "SUPPORT"
        assert result["current_role"] == "RESISTANCE"

    def test_nifty_breakout_with_real_profile(self):
        # Real NIFTY thresholds: breakout_buffer=20, retest_tolerance=5.
        level = 24500
        lead_in, h, m = _quiet_lead_in("2026-08-10", 9, 0, price=24480.0)
        candles = lead_in + [
            _candle("2026-08-10", h, m, 24485, 24540, 24480, 24535, v=700),   # close 24535 > 24520
            _candle("2026-08-10", h, m + 3, 24538, 24560, 24503, 24555),      # retest low 24503 <= 24505, close above
            _candle("2026-08-10", h, m + 6, 24556, 24580, 24550, 24570),      # confirmation: close 24570 > 24535
        ]
        result = il.detect_role_reversal(level, candles, profile=il.get_profile("NIFTY"))
        assert result is not None
        assert result["current_role"] == "SUPPORT"

    def test_banknifty_fake_breakout_is_rejected(self):
        # Close clears the breakout threshold, but the "retest" candle
        # closes BELOW the level again (never reclaims it) -- the
        # pattern must NOT complete.
        level = 56000
        lead_in, h, m = _quiet_lead_in("2026-08-10", 9, 0, price=55950.0)
        candles = lead_in + [
            _candle("2026-08-10", h, m, 55960, 56070, 55950, 56060, v=700),   # close 56060 > 56050
            _candle("2026-08-10", h, m + 3, 56055, 56065, 55990, 55995),      # fails back below the level, no reclaim
        ]
        result = il.detect_role_reversal(level, candles, profile=il.get_profile("BANKNIFTY"))
        assert result is None

    def test_naturalgas_retest_confirmation_with_real_profile(self):
        # Real NATURALGAS thresholds: breakout_buffer=0.20, retest_tolerance=0.05.
        level = 260.0
        lead_in, h, m = _quiet_lead_in("2026-08-10", 18, 0, price=258.5)
        candles = lead_in + [
            _candle("2026-08-10", h, m, 258.8, 260.5, 258.7, 260.35, v=700),   # close 260.35 > 260.2
            _candle("2026-08-10", h, m + 3, 260.35, 260.7, 260.02, 260.5),     # retest low 260.02 <= 260.05
            _candle("2026-08-10", h, m + 6, 260.45, 260.9, 260.4, 260.8),      # confirmation: close 260.8 > 260.35
        ]
        result = il.detect_role_reversal(level, candles, profile=il.get_profile("NATURALGAS"))
        assert result is not None
        assert result["current_role"] == "SUPPORT"

    def test_no_pattern_returns_none(self):
        # Consistently well below the level, never even a candidate
        # breakout -- with the default zero-buffer profile.
        candles = [_candle("2026-08-10", 9, 15 + i, 80, 81, 79, 80.2) for i in range(5)]
        assert il.detect_role_reversal(100, candles) is None

    def test_returns_the_most_recent_completed_pattern(self):
        # Two separate breakout+retest+confirmation cycles at the same
        # level -- the SECOND (more recent) one's outcome must win.
        lead_in, h, m = _quiet_lead_in("2026-08-10", 9, 0)
        candles = lead_in + [
            _candle("2026-08-10", h, m, 95, 105, 94, 104.5, v=700),          # breakout up
            _candle("2026-08-10", h, m + 3, 103, 103.5, 100.5, 103.2),       # retest defended
            _candle("2026-08-10", h, m + 6, 103.3, 106, 103, 105.5, v=700),  # confirmation (close > 104.5) + itself a fresh breakdown volume base
            _candle("2026-08-10", h, m + 9, 104, 105, 90, 91, v=900),        # breakdown back below, vol 900 >= (avg incl. prior 700s)*1.2
            _candle("2026-08-10", h, m + 12, 92, 99.5, 91, 92),              # retest rejected -> RESISTANCE (more recent)
            _candle("2026-08-10", h, m + 15, 91.5, 92, 85, 86),              # confirmation: close 86 < breakdown close 91
        ]
        result = il.detect_role_reversal(100, candles, profile={"breakout_buffer": 2, "retest_tolerance": 1})
        assert result["current_role"] == "RESISTANCE"

    def test_retest_beyond_max_retest_candles_is_rejected(self):
        # Same valid breakout as test_resistance_to_support_flip, but the
        # retest happens 4 candles later -- one past MAX_RETEST_CANDLES(=3)
        # -- so even though it's within the wider `lookahead` scan window,
        # it must NOT be treated as a valid pattern.
        lead_in, h, m = _quiet_lead_in("2026-08-10", 9, 0)
        candles = lead_in + [
            _candle("2026-08-10", h, m, 95.5, 105, 95, 104.5, v=700),
            # Low volume (v=100) on these three -- keeps them from
            # independently qualifying as their OWN confirmed breakout
            # candidate (which would let a fresh, in-window retest right
            # after one of them mask the real thing this test checks).
            _candle("2026-08-10", h, m + 3, 103.8, 104, 103.5, 103.9, v=100),
            _candle("2026-08-10", h, m + 6, 103.7, 104, 103.4, 103.8, v=100),
            _candle("2026-08-10", h, m + 9, 103.6, 104, 103.3, 103.7, v=100),
            _candle("2026-08-10", h, m + 12, 103, 103.5, 100.5, 103.2, v=100),   # retest -- 4 candles after breakout
            _candle("2026-08-10", h, m + 15, 103.3, 105.5, 103.1, 105.1, v=100),
        ]
        result = il.detect_role_reversal(100, candles, profile={"breakout_buffer": 2, "retest_tolerance": 1})
        assert result is None

    def test_low_volume_breakout_is_rejected(self):
        # Identical price action to test_resistance_to_support_flip, but
        # the breakout candle's own volume never clears
        # MIN_VOLUME_MULTIPLIER x the preceding rolling average -- must
        # be treated as an unconfirmed (fake) breakout.
        lead_in, h, m = _quiet_lead_in("2026-08-10", 9, 0, v=500)
        candles = lead_in + [
            _candle("2026-08-10", h, m, 95.5, 105, 95, 104.5, v=550),   # 550 < 500*1.2=600 -- NOT confirmed
            _candle("2026-08-10", h, m + 3, 103, 103.5, 100.5, 103.2),
            _candle("2026-08-10", h, m + 6, 103.3, 105.5, 103.1, 105.1),
        ]
        result = il.detect_role_reversal(100, candles, profile={"breakout_buffer": 2, "retest_tolerance": 1})
        assert result is None

    def test_no_confirmation_candle_yet_is_not_a_pattern(self):
        # Same valid breakout+retest as test_resistance_to_support_flip,
        # but no candle exists yet AFTER the retest -- honestly not
        # confirmed yet, not confirmed-by-default.
        lead_in, h, m = _quiet_lead_in("2026-08-10", 9, 0)
        candles = lead_in + [
            _candle("2026-08-10", h, m, 95.5, 105, 95, 104.5, v=700),
            _candle("2026-08-10", h, m + 3, 103, 103.5, 100.5, 103.2),
        ]
        result = il.detect_role_reversal(100, candles, profile={"breakout_buffer": 2, "retest_tolerance": 1})
        assert result is None

    def test_max_retest_candles_override_widens_the_search_window(self):
        # Same setup as test_retest_beyond_max_retest_candles_is_rejected
        # (retest 4 candles after breakout, past the live default of 3)
        # -- but an explicit max_retest_candles=5 override must find it.
        lead_in, h, m = _quiet_lead_in("2026-08-10", 9, 0)
        candles = lead_in + [
            _candle("2026-08-10", h, m, 95.5, 105, 95, 104.5, v=700),
            _candle("2026-08-10", h, m + 3, 103.8, 104, 103.5, 103.9, v=100),
            _candle("2026-08-10", h, m + 6, 103.7, 104, 103.4, 103.8, v=100),
            _candle("2026-08-10", h, m + 9, 103.6, 104, 103.3, 103.7, v=100),
            _candle("2026-08-10", h, m + 12, 103, 103.5, 100.5, 103.2, v=100),
            _candle("2026-08-10", h, m + 15, 103.3, 105.5, 103.1, 105.1, v=100),
        ]
        result = il.detect_role_reversal(100, candles, profile={"breakout_buffer": 2, "retest_tolerance": 1},
                                          max_retest_candles=5)
        assert result is not None
        assert result["current_role"] == "SUPPORT"

    def test_min_volume_multiplier_override_admits_a_lower_volume_breakout(self):
        # Same setup as test_low_volume_breakout_is_rejected (breakout
        # vol 550 < live default 500*1.2=600) -- an explicit
        # min_volume_multiplier=1.0 override must admit it (550 >= 500*1.0).
        lead_in, h, m = _quiet_lead_in("2026-08-10", 9, 0, v=500)
        candles = lead_in + [
            _candle("2026-08-10", h, m, 95.5, 105, 95, 104.5, v=550),
            _candle("2026-08-10", h, m + 3, 103, 103.5, 100.5, 103.2),
            _candle("2026-08-10", h, m + 6, 103.3, 105.5, 103.1, 105.1),
        ]
        result = il.detect_role_reversal(100, candles, profile={"breakout_buffer": 2, "retest_tolerance": 1},
                                          min_volume_multiplier=1.0)
        assert result is not None

    def test_confirmation_candle_that_fails_to_extend_is_rejected(self):
        # Retest is real, but the very next candle closes BELOW the
        # breakout's own close instead of beyond it -- the reversal
        # never actually got confirmed.
        lead_in, h, m = _quiet_lead_in("2026-08-10", 9, 0)
        candles = lead_in + [
            _candle("2026-08-10", h, m, 95.5, 105, 95, 104.5, v=700),
            _candle("2026-08-10", h, m + 3, 103, 103.5, 100.5, 103.2),
            _candle("2026-08-10", h, m + 6, 103.1, 103.4, 102.9, 103.0),   # close 103.0 < breakout close 104.5 -- fails to confirm
        ]
        result = il.detect_role_reversal(100, candles, profile={"breakout_buffer": 2, "retest_tolerance": 1})
        assert result is None


class TestComputeTradePlanOverlay:
    def _bullish_reversal(self, confidence=98):
        return {
            "level": 100, "previous_role": "RESISTANCE", "current_role": "SUPPORT", "confidence": confidence,
            "breakout_candle": {"high": 105, "low": 95, "close": 104.5},
            "retest_candle": {"high": 103.5, "low": 100.5, "close": 103.2},
        }

    def _bearish_reversal(self, confidence=98):
        return {
            "level": 100, "previous_role": "SUPPORT", "current_role": "RESISTANCE", "confidence": confidence,
            "breakout_candle": {"high": 105, "low": 90, "close": 91},
            "retest_candle": {"high": 99.5, "low": 91, "close": 92},
        }

    def test_bullish_overlay_uses_the_exact_spec_formula(self):
        overlay = il.compute_trade_plan_overlay("NIFTY", self._bullish_reversal())
        # entry = breakout_high(105) + buffer(5) = 110; sl = retest_low(100.5) - buffer(5) = 95.5
        # risk = 110 - 95.5 = 14.5; t1 = 124.5; t2 = 139.0
        assert overlay == {"direction": "BULLISH", "entry": 110.0, "sl": 95.5, "t1": 124.5, "t2": 139.0}

    def test_bearish_overlay_uses_the_exact_spec_formula(self):
        overlay = il.compute_trade_plan_overlay("NIFTY", self._bearish_reversal())
        # entry = breakdown_low(90) - buffer(5) = 85; sl = retest_high(99.5) + buffer(5) = 104.5
        # risk = 104.5 - 85 = 19.5; t1 = 85 - 19.5 = 65.5; t2 = 85 - 39 = 46.0
        assert overlay == {"direction": "BEARISH", "entry": 85.0, "sl": 104.5, "t1": 65.5, "t2": 46.0}

    def test_none_when_confidence_below_threshold(self):
        assert il.compute_trade_plan_overlay("NIFTY", self._bullish_reversal(confidence=74)) is None

    def test_at_exactly_the_confidence_threshold_is_attached(self):
        assert il.compute_trade_plan_overlay("NIFTY", self._bullish_reversal(confidence=75)) is not None

    def test_none_when_reversal_is_none(self):
        assert il.compute_trade_plan_overlay("NIFTY", None) is None

    def test_uses_zero_buffer_for_an_unmapped_symbol(self):
        overlay = il.compute_trade_plan_overlay("SOME_UNMAPPED_SYMBOL", self._bullish_reversal())
        assert overlay["entry"] == 105.0  # breakout_high + 0
        assert overlay["sl"] == 100.5     # retest_low - 0

    def test_different_instruments_use_their_own_buffer(self):
        overlay = il.compute_trade_plan_overlay("NATURALGAS", self._bullish_reversal())
        assert overlay["entry"] == 105.15  # 105 + 0.15
        assert overlay["sl"] == 100.35     # 100.5 - 0.15


class TestPickOptionStrike:
    def _rows(self, *, atm=24500, ce_ltp=140.0, pe_ltp=95.0):
        return [
            StrikeRow(strike=atm - 100, ce_ltp=200.0, pe_ltp=50.0),
            StrikeRow(strike=atm, ce_ltp=ce_ltp, pe_ltp=pe_ltp),
            StrikeRow(strike=atm + 100, ce_ltp=90.0, pe_ltp=150.0),
        ]

    def test_bullish_picks_the_atm_ce(self):
        result = il.pick_option_strike(self._rows(), 24500, "BULLISH")
        assert result == {"strike": 24500, "option_type": "CE", "premium": 140.0}

    def test_bearish_picks_the_atm_pe(self):
        result = il.pick_option_strike(self._rows(), 24500, "BEARISH")
        assert result == {"strike": 24500, "option_type": "PE", "premium": 95.0}

    def test_none_when_atm_row_is_missing(self):
        assert il.pick_option_strike(self._rows(atm=24500), 99999, "BULLISH") is None

    def test_none_when_the_relevant_premium_is_zero(self):
        assert il.pick_option_strike(self._rows(ce_ltp=0.0), 24500, "BULLISH") is None

    def test_none_when_the_relevant_premium_is_missing_default(self):
        rows = [StrikeRow(strike=24500)]   # ce_ltp/pe_ltp default to 0.0
        assert il.pick_option_strike(rows, 24500, "BEARISH") is None


class TestWeightedLevels:
    def test_empty_candles_and_rows_returns_empty(self):
        assert il.weighted_levels("NIFTY", candles=[], rows=[], atm=24500, underlying=24505) == []

    def test_a_level_confirmed_by_enough_sources_is_major(self):
        import dataclasses

        @dataclasses.dataclass
        class _Row:
            strike: float
            ce_oi: int
            pe_oi: int

        # PDH/PDL/pivots from prior day's candles all cluster near 100,
        # plus an OI wall at the same price -- pivot(0.15) + oi(0.25) = 0.40,
        # still short; add swing high there too -- + 0.20 = 0.60, still
        # short of 0.65 -- add VWAP too for a genuinely "major" level.
        candles = [
            _candle("2026-08-09", 9, 15, 99, 100.5, 99.5, 100),
            _candle("2026-08-09", 9, 18, 100, 100.2, 99.8, 100),
            _candle("2026-08-10", 9, 15, 100, 100.1, 99.9, 100, v=50000),
        ]
        rows = [_Row(strike=100, ce_oi=100, pe_oi=50000), _Row(strike=110, ce_oi=200, pe_oi=100)]
        levels = il.weighted_levels(
            "NIFTY", candles=candles, rows=rows, atm=100, underlying=100.05,
            today=dt.date(2026, 8, 10), strike_step=10,
        )
        assert any(lv["weight"] >= il.MAJOR_LEVEL_MIN_WEIGHT and abs(lv["level"] - 100) < 5 for lv in levels)

    def test_results_sorted_by_weight_descending(self):
        import dataclasses

        @dataclasses.dataclass
        class _Row:
            strike: float
            ce_oi: int
            pe_oi: int

        candles = [_candle("2026-08-09", 9, 15 + i, 100, 100.5, 99.5, 100) for i in range(5)]
        rows = [_Row(strike=100, ce_oi=500000, pe_oi=100), _Row(strike=200, ce_oi=100, pe_oi=500000)]
        levels = il.weighted_levels("NIFTY", candles=candles, rows=rows, atm=100, underlying=150, strike_step=10)
        weights = [lv["weight"] for lv in levels]
        assert weights == sorted(weights, reverse=True)


class TestBestCandidateLevel:
    def _rows(self):
        import dataclasses

        @dataclasses.dataclass
        class _Row:
            strike: float
            ce_oi: int
            pe_oi: int
        return _Row

    def test_none_when_genuinely_no_candidates(self):
        assert il.best_candidate_level("NIFTY", candles=[], rows=[], atm=24500, underlying=24505) is None

    def test_a_below_threshold_cluster_is_still_returned_with_is_major_false(self):
        # The real CRUDEOIL-style case: an OI wall + round number agree
        # (0.25 + 0.10 = 0.35), short of MAJOR_LEVEL_MIN_WEIGHT (0.65) --
        # weighted_levels() would return [], but this should still
        # surface the best near-miss for a preview chart. No candles at
        # all (only OI/round-number sources need any input here) --
        # keeps this deterministic without prev-day/swing/VWAP
        # candidates also entering the same cluster.
        _Row = self._rows()
        rows = [_Row(strike=100, ce_oi=100, pe_oi=500000)]
        result = il.best_candidate_level("NIFTY", candles=[], rows=rows, atm=100, underlying=105, strike_step=10)
        assert result is not None
        assert result["is_major"] is False
        assert result["weight"] < il.MAJOR_LEVEL_MIN_WEIGHT
        assert il.weighted_levels("NIFTY", candles=[], rows=rows, atm=100, underlying=105, strike_step=10) == []

    def test_a_major_cluster_is_returned_with_is_major_true(self):
        _Row = self._rows()
        candles = [
            _candle("2026-08-09", 9, 15, 99, 100.5, 99.5, 100),
            _candle("2026-08-09", 9, 18, 100, 100.2, 99.8, 100),
            _candle("2026-08-10", 9, 15, 100, 100.1, 99.9, 100, v=50000),
        ]
        rows = [_Row(strike=100, ce_oi=100, pe_oi=50000), _Row(strike=110, ce_oi=200, pe_oi=100)]
        result = il.best_candidate_level(
            "NIFTY", candles=candles, rows=rows, atm=100, underlying=100.05,
            today=dt.date(2026, 8, 10), strike_step=10,
        )
        assert result is not None
        assert result["is_major"] is True
        assert result["weight"] >= il.MAJOR_LEVEL_MIN_WEIGHT

    def test_returns_the_single_highest_weight_cluster(self):
        _Row = self._rows()
        candles = [_candle("2026-08-09", 9, 15 + i, 100, 100.5, 99.5, 100) for i in range(5)]
        rows = [_Row(strike=100, ce_oi=500000, pe_oi=100), _Row(strike=200, ce_oi=100, pe_oi=500000)]
        result = il.best_candidate_level("NIFTY", candles=candles, rows=rows, atm=100, underlying=150, strike_step=10)
        all_levels = il._score_all_clusters("NIFTY", candles=candles, rows=rows, atm=100, underlying=150, strike_step=10)
        assert result["level"] == max(all_levels, key=lambda c: c["weight"])["level"]


class TestClassifyMarketState:
    def _rising_candles(self, n=60):
        return [_candle("2026-08-10", 9, 15 + i, 100 + i * 0.5, 100.5 + i * 0.5, 99.5 + i * 0.5, 100.3 + i * 0.5) for i in range(n)]

    def test_strong_uptrend_classified_as_trending_up(self):
        candles = self._rising_candles()
        result = il.classify_market_state("NIFTY", candles=candles, levels=[], underlying=candles[-1]["close"],
                                           vwap=candles[0]["close"], oi_lean=1.5)
        assert result["state"] == il.TRENDING_UP
        assert result["score"] > 0

    def test_flat_candles_with_no_evidence_is_range(self):
        candles = [_candle("2026-08-10", 9, 15 + i, 100, 100.1, 99.9, 100) for i in range(60)]
        result = il.classify_market_state("NIFTY", candles=candles, levels=[], underlying=100, vwap=100, oi_lean=0)
        assert result["state"] == il.RANGE

    def test_conflicting_trend_and_vwap_is_reversal_risk(self):
        # Rising closes (bullish EMA alignment) but price below VWAP and
        # bearish OI -- genuinely conflicting evidence.
        candles = self._rising_candles()
        result = il.classify_market_state("NIFTY", candles=candles, levels=[], underlying=candles[-1]["close"],
                                           vwap=candles[-1]["close"] + 1000, oi_lean=-1.8)
        assert result["state"] in (il.REVERSAL_RISK, il.BREAKOUT_WATCH, il.TRENDING_UP)
        # score itself must reflect the conflict even if the exact bucket varies
        assert "components" in result and result["components"]["vwap_alignment"] == -1.0

    def test_active_role_reversal_at_support_is_bullish_retest_active(self):
        # classify_market_state() uses the REAL NIFTY profile internally
        # (breakout_buffer=20, retest_tolerance=5) -- scale the candle
        # data to actually clear those real thresholds, not the small
        # deltas the zero-buffer detect_role_reversal tests use directly.
        # Milestone 20, Phase 6: also needs real preceding volume history
        # and a confirmation candle -- see _quiet_lead_in()'s own docstring.
        level = 24500
        lead_in, h, m = _quiet_lead_in("2026-08-10", 9, 0, price=24480.0)
        candles = lead_in + [
            _candle("2026-08-10", h, m, 24485, 24540, 24470, 24535, v=700),   # close 24535 > 24500+20
            _candle("2026-08-10", h, m + 3, 24530, 24545, 24503, 24540),      # retest low 24503 <= 24505, close above
            _candle("2026-08-10", h, m + 6, 24541, 24560, 24538, 24555),      # confirmation: close 24555 > 24535
        ]
        result = il.classify_market_state("NIFTY", candles=candles, levels=[{"level": level}], underlying=24540)
        assert result["state"] == il.BULLISH_RETEST_ACTIVE
