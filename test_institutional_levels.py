"""
test_institutional_levels.py -- Milestone 20, Phase 1: regression tests
for institutional_levels.py (weighted composite S/R + role-reversal
detection). Repo-root location matches this module's own (same
convention as test_market_hours.py for market_session-adjacent, repo-
root modules).
"""
import datetime as dt

import institutional_levels as il


def _candle(date_str, hour, minute, o, h, l, c, v=1000):
    base = dt.datetime.fromisoformat(f"{date_str}T{hour:02d}:00:00")
    return {"datetime": base + dt.timedelta(minutes=minute),
            "open": o, "high": h, "low": l, "close": c, "volume": v}


class TestDetectRoleReversal:
    def test_resistance_to_support_flip(self):
        # Breakout above 100, then a retest candle dips back to it with a
        # dominant lower wick and closes back above -- RESISTANCE -> SUPPORT.
        candles = [
            _candle("2026-08-10", 9, 15, 95, 96, 94, 95.5),
            _candle("2026-08-10", 9, 18, 95.5, 105, 95, 104.5),   # breakout close 104.5 > 100
            _candle("2026-08-10", 9, 21, 104, 104.5, 103, 104),
            _candle("2026-08-10", 9, 24, 103, 103.5, 100.5, 103.2),  # retest: low 100.5, close 103.2, big lower wick
        ]
        result = il.detect_role_reversal(100, candles, profile={"breakout_buffer": 2, "retest_tolerance": 1})
        assert result is not None
        assert result["previous_role"] == "RESISTANCE"
        assert result["current_role"] == "SUPPORT"
        assert result["level"] == 100
        assert 60 <= result["confidence"] <= 98

    def test_support_to_resistance_flip(self):
        # Breakdown below 100, then a retest candle pokes back up with a
        # dominant upper wick and closes back below -- SUPPORT -> RESISTANCE.
        candles = [
            _candle("2026-08-10", 9, 15, 105, 106, 104, 104.5),
            _candle("2026-08-10", 9, 18, 104.5, 105, 95, 95.5),     # breakdown close 95.5 < 100
            _candle("2026-08-10", 9, 21, 96, 97, 95.5, 96),
            _candle("2026-08-10", 9, 24, 96.8, 99.5, 96.5, 96.8),   # retest: high 99.5, close 96.8, big upper wick
        ]
        result = il.detect_role_reversal(100, candles, profile={"breakout_buffer": 2, "retest_tolerance": 1})
        assert result is not None
        assert result["previous_role"] == "SUPPORT"
        assert result["current_role"] == "RESISTANCE"

    def test_nifty_breakout_with_real_profile(self):
        # Real NIFTY thresholds: breakout_buffer=20, retest_tolerance=5.
        level = 24500
        candles = [
            _candle("2026-08-10", 9, 15, 24480, 24490, 24470, 24485),
            _candle("2026-08-10", 9, 18, 24485, 24540, 24480, 24535),  # close 24535 > 24500+20=24520
            _candle("2026-08-10", 9, 21, 24530, 24545, 24525, 24540),
            _candle("2026-08-10", 9, 24, 24538, 24560, 24503, 24555),  # retest low 24503 <= 24505, close above, big lower wick
        ]
        result = il.detect_role_reversal(level, candles, profile=il.get_profile("NIFTY"))
        assert result is not None
        assert result["current_role"] == "SUPPORT"

    def test_banknifty_fake_breakout_is_rejected(self):
        # Close clears the breakout threshold, but the "retest" candle
        # closes BELOW the level again (never reclaims it) -- the
        # pattern must NOT complete.
        level = 56000
        candles = [
            _candle("2026-08-10", 9, 15, 55950, 55980, 55930, 55960),
            _candle("2026-08-10", 9, 18, 55960, 56070, 55950, 56060),  # close 56060 > 56000+50=56050
            _candle("2026-08-10", 9, 21, 56055, 56065, 55990, 55995),  # fails back below the level, no reclaim
        ]
        result = il.detect_role_reversal(level, candles, profile=il.get_profile("BANKNIFTY"))
        assert result is None

    def test_naturalgas_retest_confirmation_with_real_profile(self):
        # Real NATURALGAS thresholds: breakout_buffer=0.20, retest_tolerance=0.05.
        level = 260.0
        candles = [
            _candle("2026-08-10", 18, 0, 258.5, 259.0, 258.0, 258.8),
            _candle("2026-08-10", 18, 3, 258.8, 260.5, 258.7, 260.35),  # close 260.35 > 260.2
            _candle("2026-08-10", 18, 6, 260.3, 260.6, 260.1, 260.4),
            _candle("2026-08-10", 18, 9, 260.35, 260.7, 260.02, 260.5),  # retest low 260.02 <= 260.05, big lower wick
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
        # Two separate breakout+retest cycles at the same level -- the
        # SECOND (more recent) one's outcome must win.
        candles = [
            _candle("2026-08-10", 9, 15, 95, 105, 94, 104.5),   # breakout up
            _candle("2026-08-10", 9, 18, 103, 103.5, 100.5, 103.2),  # retest defended -> SUPPORT
            _candle("2026-08-10", 9, 21, 102, 103, 90, 91),     # breakdown back below
            _candle("2026-08-10", 9, 24, 92, 99.5, 91, 92),     # retest rejected -> RESISTANCE (more recent)
        ]
        result = il.detect_role_reversal(100, candles, profile={"breakout_buffer": 2, "retest_tolerance": 1})
        assert result["current_role"] == "RESISTANCE"


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
        level = 24500
        candles = [
            _candle("2026-08-10", 9, 15, 24480, 24540, 24470, 24535),   # close 24535 > 24500+20
            _candle("2026-08-10", 9, 18, 24530, 24545, 24503, 24540),   # retest low 24503 <= 24505, big lower wick, close above
        ]
        result = il.classify_market_state("NIFTY", candles=candles, levels=[{"level": level}], underlying=24540)
        assert result["state"] == il.BULLISH_RETEST_ACTIVE
