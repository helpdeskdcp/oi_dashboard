"""
test_sr_engine_v3.py -- regression tests for sr_engine_v3.py (Support &
Resistance Engine V3: dynamic S/R + OI-cluster + Greeks + previous-day
validation/extension lifecycle).

Synthetic data only -- no live broker/market dependency, same philosophy as
test_engine.py / test_scalping_engine.py.
"""
import datetime as dt

from oi_engine import StrikeRow
from market_structure import classical_pivots
from sr_engine_v3 import (
    compute_v3_raw_levels, map_levels_to_strikes, analyze_oi_cluster,
    v3_regime_weights, classify_volatility, compute_hold_break_probability, compute_v3_confidence,
    build_level_ladder, select_best_level,
    compute_oi_cluster_center, detect_wall_migration,
    validate_previous_day_levels, classify_level_outcome,
    detect_resistance_extend_up, detect_support_extend_down,
    should_pause_time_exit, confirms_reversal_entry,
    learn_adaptive_weights, V3_DEFAULT_FACTOR_WEIGHTS,
    generate_v3_signal,
)

STEP = 50


def _flat_rows(low=24500, high=24950, step=STEP):
    rows = []
    for k in range(low, high, step):
        r = StrikeRow(strike=k)
        r.ce_oi = r.pe_oi = 100000
        r.ce_vol = r.pe_vol = 5000
        r.ce_ltp = r.pe_ltp = 100.0
        r.ce_iv = r.pe_iv = 18.0
        r.ce_delta, r.pe_delta = 0.4, -0.4
        r.ce_buy_qty, r.ce_sell_qty = 1000, 500
        r.pe_buy_qty, r.pe_sell_qty = 500, 1000
        rows.append(r)
    return rows


class TestComputeV3RawLevels:
    def test_missing_structure_returns_none_not_fabricated(self):
        levels = compute_v3_raw_levels(None, 24700)
        assert levels["dynamic_resistance"] is None
        assert levels["dynamic_support"] is None
        assert levels["resistance_completeness"] == 0.0

    def test_full_structure_produces_weighted_levels(self):
        ms = {
            "swing_high": 24800, "swing_low": 24600, "vwap": 24690,
            "pivots": {"R1": 24800, "S1": 24600},
            "cpr": {"tc": 24720, "bc": 24680},
            "custom_levels": {"resistance": 24800, "support": 24600},
        }
        levels = compute_v3_raw_levels(ms, 24700)
        assert levels["dynamic_resistance"] is not None
        assert levels["dynamic_support"] is not None
        # VWAP (24690) sits BELOW underlying (24700) here, so it legitimately
        # counts toward support_completeness instead of resistance_completeness
        # (see compute_v3_raw_levels' "tested from one side" note) -- resistance
        # still gets PDH+SwingHigh+PivotR1+CPR-TC, i.e. everything but VWAP.
        assert levels["resistance_completeness"] >= 0.8
        assert levels["support_completeness"] == 1.0

    def test_vwap_only_counts_on_the_side_price_is_testing_from(self):
        ms = {"vwap": 24690}
        below = compute_v3_raw_levels(ms, 24600)   # price below VWAP -> VWAP is resistance
        above = compute_v3_raw_levels(ms, 24800)   # price above VWAP -> VWAP is support
        assert below["dynamic_resistance"] == 24690
        assert below["dynamic_support"] is None
        assert above["dynamic_support"] == 24690
        assert above["dynamic_resistance"] is None


class TestMapLevelsToStrikes:
    def test_floors_support_ceils_resistance(self):
        support, resistance = map_levels_to_strikes(24625.14, 24785.0, STEP)
        assert support == 24600   # floor
        assert resistance == 24800   # ceil

    def test_none_input_returns_none(self):
        assert map_levels_to_strikes(None, None, STEP) == (None, None)


class TestClassicalPivotsExtended:
    def test_r4_s4_present_and_ordered(self):
        p = classical_pivots(24800, 24600, 24700)
        assert "R4" in p and "S4" in p
        assert p["R4"] > p["R3"] > p["R2"] > p["R1"]
        assert p["S4"] < p["S3"] < p["S2"] < p["S1"]

    def test_r4_s4_extension_formula(self):
        p = classical_pivots(24800, 24600, 24700)
        assert p["R4"] == round(p["R3"] + (p["R2"] - p["R1"]), 2)
        assert p["S4"] == round(p["S3"] - (p["S1"] - p["S2"]), 2)


class TestLevelLadder:
    def _ms(self):
        return {
            "pivots": {"R1": 24750, "R2": 24800, "R3": 24850, "R4": 24900,
                       "S1": 24650, "S2": 24600, "S3": 24550, "S4": 24500},
            "custom_levels": {"resistance": 24950, "support": 24450},
        }

    def test_ladder_sorted_by_distance_from_underlying(self):
        ladder = build_level_ladder(self._ms(), underlying=24700, strike_step=STEP)
        res_strikes = [c["strike"] for c in ladder["resistance_candidates"]]
        assert res_strikes == sorted(res_strikes, key=lambda s: abs(s - 24700))
        sup_strikes = [c["strike"] for c in ladder["support_candidates"]]
        assert sup_strikes == sorted(sup_strikes, key=lambda s: abs(s - 24700))

    def test_ladder_includes_dynamic_rung(self):
        ladder = build_level_ladder(self._ms(), underlying=24700, strike_step=STEP)
        labels = {c["label"] for c in ladder["resistance_candidates"]}
        assert "DYNAMIC" in labels
        assert {"R1", "R2", "R3", "R4"}.issubset(labels)

    def test_select_best_level_picks_the_rung_with_real_oi_not_just_nearest(self):
        # R1 (nearest to price) has NO OI data at its mapped strike this cycle
        # (outside the fetched window); a farther rung (R3) has a genuine wall.
        # select_best_level must pick R3, not blindly default to the nearest R1.
        rows = [StrikeRow(strike=k) for k in range(24500, 24950, STEP)]
        for r in rows:
            r.ce_oi, r.ce_vol = 100000, 5000
        wall_row = next(r for r in rows if r.strike == 24850)   # R3 maps here
        wall_row.ce_oi, wall_row.ce_oi_chg = 900000, 50000
        ms = self._ms()
        best = select_best_level(rows, ms, underlying=24700, strike_step=STEP, direction="resistance")
        assert best is not None
        assert best["strike"] == 24850
        assert best["cluster"]["tradeable_data"] is True

    def test_select_best_level_falls_back_to_nearest_when_nothing_tradeable(self):
        best = select_best_level([], self._ms(), underlying=24700, strike_step=STEP, direction="resistance")
        assert best is not None
        assert best["cluster"]["tradeable_data"] is False

    def test_select_best_level_none_when_no_candidates(self):
        assert select_best_level([], {}, underlying=24700, strike_step=STEP, direction="resistance") is None


class TestAnalyzeOiCluster:
    def test_no_rows_in_cluster_returns_untradeable(self):
        result = analyze_oi_cluster([], 24600, "PE", STEP)
        assert result["tradeable_data"] is False

    def test_none_center_strike_returns_none(self):
        assert analyze_oi_cluster(_flat_rows(), None, "PE", STEP) is None

    def test_concentrated_wall_scores_higher_than_flat_cluster(self):
        rows = _flat_rows()
        wall_row = next(r for r in rows if r.strike == 24600)
        wall_row.pe_oi = 900000
        wall_row.pe_oi_chg = 50000
        wall_eval = analyze_oi_cluster(rows, 24600, "PE", STEP)
        flat_eval = analyze_oi_cluster(rows, 24700, "PE", STEP)
        assert wall_eval["institutional_wall_score"] > flat_eval["institutional_wall_score"]
        assert wall_eval["support_resistance_strength"] > flat_eval["support_resistance_strength"]

    def test_unwinding_oi_raises_fake_index(self):
        # Compare unwinding vs fresh-writing at the SAME wall size/liquidity --
        # unwinding must score a meaningfully higher fake_index than fresh
        # writing, even if liquidity/IV pull the absolute number down.
        rows_unwinding = _flat_rows()
        row_u = next(r for r in rows_unwinding if r.strike == 24600)
        row_u.pe_oi, row_u.pe_oi_chg = 900000, -80000
        unwinding_eval = analyze_oi_cluster(rows_unwinding, 24600, "PE", STEP)

        rows_writing = _flat_rows()
        row_w = next(r for r in rows_writing if r.strike == 24600)
        row_w.pe_oi, row_w.pe_oi_chg = 900000, 80000
        writing_eval = analyze_oi_cluster(rows_writing, 24600, "PE", STEP)

        assert unwinding_eval["fake_index"] > writing_eval["fake_index"]

    def test_liquidity_absent_degrades_gracefully(self):
        rows = _flat_rows()
        for r in rows:
            r.pe_buy_qty = r.pe_sell_qty = 0
        result = analyze_oi_cluster(rows, 24600, "PE", STEP)
        assert result["liquidity_score"] is None
        assert result["data_completeness"] < 1.0   # honestly lowered, not faked


class TestRegimeWeightsAndProbabilities:
    def test_trending_favors_break(self):
        w = v3_regime_weights({"regime": "TRENDING"})
        assert w["break_mult"] > 1.0
        assert w["hold_mult"] < 1.0

    def test_ranging_favors_hold(self):
        w = v3_regime_weights({"regime": "RANGING"})
        assert w["hold_mult"] > 1.0

    def test_unknown_regime_is_neutral(self):
        assert v3_regime_weights(None) == v3_regime_weights({"regime": "UNKNOWN"})

    def test_hold_break_probability_bounded(self):
        rows = _flat_rows()
        row = next(r for r in rows if r.strike == 24600)
        row.pe_oi, row.pe_oi_chg = 900000, 50000
        cluster = analyze_oi_cluster(rows, 24600, "PE", STEP)
        hold_pct, break_pct = compute_hold_break_probability(cluster, v3_regime_weights({}))
        assert 5 <= hold_pct <= 95
        assert round(hold_pct + break_pct, 1) == 100.0

    def test_missing_cluster_returns_none_none(self):
        assert compute_hold_break_probability(None, v3_regime_weights({})) == (None, None)

    def test_confidence_scaled_down_by_completeness(self):
        rows = _flat_rows()
        row = next(r for r in rows if r.strike == 24600)
        row.pe_oi, row.pe_oi_chg = 900000, 50000
        cluster = analyze_oi_cluster(rows, 24600, "PE", STEP)
        full = compute_v3_confidence(cluster, level_completeness=1.0)
        partial = compute_v3_confidence(cluster, level_completeness=0.2)
        assert full > partial


class TestPreviousDayValidation:
    def test_strong_wall_is_valid(self):
        rows = _flat_rows()
        row = next(r for r in rows if r.strike == 24600)
        row.pe_oi, row.pe_oi_chg = 900000, 50000
        ms = {"custom_levels": {"support": 24600}, "regime": None}
        result = validate_previous_day_levels(rows, ms, 24700, STEP)
        assert result["support"]["status"] == "VALID"

    def test_unwound_wall_is_failed(self):
        rows = _flat_rows()
        row = next(r for r in rows if r.strike == 24600)
        row.pe_oi, row.pe_oi_chg = 900000, -80000
        for r in rows:
            r.pe_buy_qty = r.pe_sell_qty = 0   # strip liquidity too -- clearly failed
        ms = {"custom_levels": {"support": 24600}, "regime": None}
        result = validate_previous_day_levels(rows, ms, 24700, STEP)
        assert result["support"]["status"] in ("WEAK", "FAILED")

    def test_no_strike_is_unknown(self):
        result = validate_previous_day_levels(_flat_rows(), {}, 24700, STEP)
        assert result["support"]["status"] == "UNKNOWN"
        assert result["resistance"]["status"] == "UNKNOWN"


class TestClassifyLevelOutcome:
    def test_never_closed_beyond_is_held(self):
        assert classify_level_outcome(24800, "resistance", [24700, 24750, 24790]) == "HELD"

    def test_closed_beyond_is_broke(self):
        assert classify_level_outcome(24800, "resistance", [24700, 24810, 24850]) == "BROKE"

    def test_broke_then_reclaimed_is_flipped(self):
        assert classify_level_outcome(24800, "resistance", [24810, 24850, 24790, 24780]) == "FLIPPED"

    def test_missing_data_is_unknown(self):
        assert classify_level_outcome(None, "resistance", [24700]) == "UNKNOWN"
        assert classify_level_outcome(24800, "resistance", []) == "UNKNOWN"


class TestConfirmsReversalEntry:
    def _candle(self, o, h, l, c):
        return {"open": o, "high": h, "low": l, "close": c}

    def test_ce_confirmed_when_support_tested_and_bounced(self):
        candles = [
            self._candle(24610, 24615, 24598, 24605),   # tested support (low within tolerance of 24600)
            self._candle(24605, 24625, 24603, 24620),   # bullish, closes above support
        ]
        assert confirms_reversal_entry("CE", 24600, candles, tolerance=5) is True

    def test_ce_blocked_when_never_tested(self):
        candles = [
            self._candle(24650, 24660, 24645, 24655),
            self._candle(24655, 24670, 24650, 24665),   # bullish but nowhere near support
        ]
        assert confirms_reversal_entry("CE", 24600, candles, tolerance=5) is False

    def test_ce_blocked_when_current_candle_not_bullish(self):
        candles = [
            self._candle(24610, 24615, 24598, 24605),   # tested support
            self._candle(24608, 24610, 24600, 24601),   # bearish close, even though above support
        ]
        assert confirms_reversal_entry("CE", 24600, candles, tolerance=5) is False

    def test_ce_blocked_when_closed_back_below_support(self):
        candles = [
            self._candle(24610, 24615, 24598, 24605),
            self._candle(24605, 24608, 24590, 24595),   # closes BELOW support -- breakdown, not a bounce
        ]
        assert confirms_reversal_entry("CE", 24600, candles, tolerance=5) is False

    def test_pe_mirrors_ce(self):
        candles = [
            self._candle(24790, 24802, 24795, 24798),   # tested resistance (high within tolerance of 24800)
            self._candle(24798, 24799, 24780, 24785),   # bearish, closes below resistance
        ]
        assert confirms_reversal_entry("PE", 24800, candles, tolerance=5) is True

    def test_missing_data_never_raises_and_blocks(self):
        assert confirms_reversal_entry("CE", None, [], tolerance=5) is False
        assert confirms_reversal_entry("CE", 24600, None, tolerance=5) is False
        assert confirms_reversal_entry("CE", 24600, [self._candle(1, 2, 0, 1)], tolerance=5) is False   # only 1 candle
        assert confirms_reversal_entry("CE", 24600, [self._candle(1, 2, 0, 1)] * 2, tolerance=None) is False


class TestShouldPauseTimeExit:
    def _candle(self, o, h, l, c):
        return {"open": o, "high": h, "low": l, "close": c}

    def test_ce_pauses_when_still_bullish_above_prev_midpoint(self):
        prev = self._candle(100, 110, 90, 95)   # midpoint = 100
        cur = self._candle(101, 108, 100, 105)  # bullish, closes above 100
        assert should_pause_time_exit("CE", prev, cur) is True

    def test_ce_does_not_pause_when_closed_below_prev_midpoint(self):
        prev = self._candle(100, 110, 90, 95)   # midpoint = 100
        cur = self._candle(99, 101, 90, 95)     # bullish but closes below 100
        assert should_pause_time_exit("CE", prev, cur) is False

    def test_ce_does_not_pause_when_current_candle_is_bearish(self):
        prev = self._candle(100, 110, 90, 95)
        cur = self._candle(108, 109, 101, 102)  # bearish (close < open) even though above midpoint
        assert should_pause_time_exit("CE", prev, cur) is False

    def test_pe_mirrors_ce(self):
        prev = self._candle(100, 110, 90, 105)   # midpoint = 100
        cur = self._candle(99, 100, 92, 95)      # bearish, closes below 100
        assert should_pause_time_exit("PE", prev, cur) is True
        cur_recovered = self._candle(96, 105, 95, 104)  # closes above midpoint -- no pause
        assert should_pause_time_exit("PE", prev, cur_recovered) is False

    def test_missing_candles_never_raises_and_does_not_pause(self):
        assert should_pause_time_exit("CE", None, None) is False
        assert should_pause_time_exit("CE", {}, {}) is False
        assert should_pause_time_exit("XX", self._candle(1, 2, 0, 1), self._candle(1, 2, 0, 1.5)) is False


class TestExtensionDetection:
    def test_full_evidence_extends(self):
        row_res = StrikeRow(strike=24800, ce_oi_chg=-80000, ce_delta=0.5)
        row_next = StrikeRow(strike=24850, ce_oi_chg=60000)
        result = detect_resistance_extend_up(
            underlying=24850, resistance_price=24800, row_at_resistance=row_res,
            row_at_next_resistance=row_next, volume_history=[1000, 1100, 1050], current_volume=5000,
            confidence=75,
        )
        assert result["extending"] is True
        assert result["evidence_count"] >= 3

    def test_no_evidence_does_not_extend(self):
        result = detect_resistance_extend_up(
            underlying=24700, resistance_price=24800, row_at_resistance=None,
            row_at_next_resistance=None, volume_history=[], current_volume=None,
        )
        assert result["extending"] is False
        assert result["evidence_count"] == 0

    def test_support_extend_down_mirrors_resistance(self):
        row_sup = StrikeRow(strike=24600, pe_oi_chg=-80000, pe_delta=-0.5)
        row_next = StrikeRow(strike=24550, pe_oi_chg=60000)
        result = detect_support_extend_down(
            underlying=24550, support_price=24600, row_at_support=row_sup,
            row_at_next_support=row_next, volume_history=[1000, 1100, 1050], current_volume=5000,
            confidence=75,
        )
        assert result["extending"] is True


class TestGenerateV3Signal:
    def test_never_raises_on_missing_market_structure(self):
        result = generate_v3_signal(_flat_rows(), 24700, None, STEP)
        assert "trade_decision" in result   # must return a dict, never raise
        assert result["trade_decision"] == "NO_TRADE"

    def test_strong_support_wall_produces_bullish_decision(self):
        rows = _flat_rows()
        row = next(r for r in rows if r.strike == 24600)
        row.pe_oi, row.pe_oi_chg = 900000, 50000
        # Confirming candles: a recent candle tested support (low near 24600),
        # and the current candle closes bullish above it -- required since the
        # 2026-08-02 fix added confirms_reversal_entry to this entry path.
        confirming_candles = [
            {"open": 24610, "high": 24615, "low": 24598, "close": 24605},
            {"open": 24700, "high": 24710, "low": 24698, "close": 24705},
        ]
        ms = {
            "atr_14": 60.0, "adx": 30.0, "regime": "TRENDING",
            "swing_high": 24800, "swing_low": 24600, "vwap": 24690,
            "pivots": {"R1": 24800, "S1": 24600}, "cpr": {"tc": 24720, "bc": 24680},
            "custom_levels": {"resistance": 24800, "support": 24600}, "recent_candles": confirming_candles,
        }
        result = generate_v3_signal(rows, 24700.0, ms, STEP,
                                     expiry_date=dt.date.today() + dt.timedelta(days=5))
        assert result["trade_decision"] == "BUY CE"
        assert result["target"] > result["suggested_entry"] > result["stop_loss"]
        assert result["risk_reward"] >= 1.4
        # Regression guard: a support-bounce CE must trade the SUPPORT strike.
        assert result["strike"] == result["support_strike"]

    def test_strong_support_wall_without_price_confirmation_is_armed_not_executed(self):
        # Same strong wall as above, but NO confirming candle data. Per the
        # 2026-08-02 upgrade, candle confirmation gates EXECUTION only, never
        # the underlying weighted decision -- so this must still report the
        # full weighted call (BUY CE, confidence, entry/target/SL all visible)
        # but stay un-executed (tradeable=False, execution_confirmed=False),
        # not silently disappear back to NO_TRADE.
        rows = _flat_rows()
        row = next(r for r in rows if r.strike == 24600)
        row.pe_oi, row.pe_oi_chg = 900000, 50000
        ms = {
            "atr_14": 60.0, "adx": 30.0, "regime": "TRENDING",
            "swing_high": 24800, "swing_low": 24600, "vwap": 24690,
            "pivots": {"R1": 24800, "S1": 24600}, "cpr": {"tc": 24720, "bc": 24680},
            "custom_levels": {"resistance": 24800, "support": 24600}, "recent_candles": [],
        }
        result = generate_v3_signal(rows, 24700.0, ms, STEP,
                                     expiry_date=dt.date.today() + dt.timedelta(days=5))
        assert result["trade_decision"] == "BUY CE"
        assert result["execution_confirmed"] is False
        assert result["tradeable"] is False
        assert "ARMED" in result["reason"]
        # Entry/target/SL/RR are still fully computed and reported even while ARMED.
        assert result["suggested_entry"] is not None
        assert result["target"] is not None
        assert result["stop_loss"] is not None

    def test_breakout_with_full_extension_evidence_favors_continuation(self):
        prev_rows = _flat_rows()
        prev_res = next(r for r in prev_rows if r.strike == 24800)
        prev_res.ce_oi, prev_res.ce_oi_chg = 900000, 50000
        prev_ms = {"custom_levels": {"support": 24600, "resistance": 24800}, "regime": None}
        prev_val = validate_previous_day_levels(prev_rows, prev_ms, 24700, STEP)

        today_rows = _flat_rows()
        r800 = next(r for r in today_rows if r.strike == 24800)
        r800.ce_oi_chg = -80000
        r800.ce_vol = 50000
        r850 = next(r for r in today_rows if r.strike == 24850)
        # Strong, unambiguous reading AT today's own resistance rung (R1=24850)
        # itself -- comfortably clears the confidence threshold regardless of
        # how the neighboring (broken) 24800 strike's volume dilutes the
        # cluster-relative volume score (added 2026-08-02).
        r850.ce_oi = 500000
        r850.ce_oi_chg = 200000
        r850.ce_vol = 20000
        r850.ce_buy_qty, r850.ce_sell_qty = 200, 2000

        ms = {
            "atr_14": 60.0, "adx": 30.0, "regime": "TRENDING",
            "swing_high": 24850, "swing_low": 24600, "vwap": 24800,
            "pivots": {"R1": 24850, "S1": 24600}, "cpr": {"tc": 24820, "bc": 24780},
            "custom_levels": {"resistance": 24850, "support": 24600}, "recent_candles": [],
        }
        result = generate_v3_signal(
            today_rows, 24850.0, ms, STEP, prev_day_validation=prev_val,
            today_ltp_history=[24700, 24750, 24790, 24810, 24830, 24850, 24860],
            volume_history_by_key={(24800, "CE"): [4000, 4200, 4100, 4300]},
            expiry_date=dt.date.today() + dt.timedelta(days=5),
        )
        assert result["today_outcome"]["resistance"] == "BROKE"
        assert result["resistance_extend_up"]["extending"] is True
        assert result["trade_decision"] == "BUY CE"
        assert result["execution_confirmed"] is True   # extension leans are pre-confirmed via their own candle-close evidence
        assert result["tradeable"] is True
        # Regression guard (2026-08-01 bug): a breakout-continuation CE must
        # trade the RESISTANCE strike, not support_strike -- callers must
        # never infer the strike from direction alone.
        assert result["strike"] == result["resistance_strike"]
        assert result["strike"] != result["support_strike"]

    def test_no_data_never_raises(self):
        result = generate_v3_signal([], 24700, {}, STEP)
        assert result["trade_decision"] == "NO_TRADE"
        assert result["tradeable"] is False

    def test_output_includes_new_institutional_fields(self):
        rows = _flat_rows()
        result = generate_v3_signal(rows, 24700.0, {}, STEP)
        for key in ("support_state", "resistance_state", "support_extend_score", "resistance_extend_score",
                    "support_wall_migration", "resistance_wall_migration",
                    "support_cluster_center", "resistance_cluster_center", "execution_confirmed"):
            assert key in result


class TestGreeksAndVolumeWeightedComponents:
    def _row(self, strike, oi=100000, oi_chg=0, vol=5000, delta=0.4, gamma=0.001, theta=-5.0, iv=18.0):
        r = StrikeRow(strike=strike, ce_oi=oi, ce_oi_chg=oi_chg, ce_vol=vol, ce_iv=iv)
        r.ce_delta, r.ce_gamma, r.ce_theta = delta, gamma, theta
        return r

    def test_volume_score_present_and_relative_to_cluster(self):
        rows = [self._row(24500 + i * STEP, vol=5000) for i in range(7)]
        rows[3].ce_vol = 50000   # center strike (24500+3*50=24650) has a big volume spike
        result = analyze_oi_cluster(rows, 24650, "CE", STEP)
        assert result["volume_score"] is not None
        assert result["volume_score"] > 50

    def test_volume_score_none_without_any_volume_data(self):
        rows = [self._row(24500 + i * STEP, vol=0) for i in range(7)]
        result = analyze_oi_cluster(rows, 24650, "CE", STEP)
        assert result["volume_score"] is None

    def test_moneyness_score_peaks_near_atm_delta(self):
        rows_atm = [self._row(24500 + i * STEP, delta=0.5) for i in range(7)]
        rows_deep_otm = [self._row(24500 + i * STEP, delta=0.05) for i in range(7)]
        atm_result = analyze_oi_cluster(rows_atm, 24650, "CE", STEP)
        otm_result = analyze_oi_cluster(rows_deep_otm, 24650, "CE", STEP)
        assert atm_result["moneyness_score"] > otm_result["moneyness_score"]

    def test_gamma_instability_feeds_fake_index(self):
        rows_calm = [self._row(24500 + i * STEP, gamma=0.001) for i in range(7)]
        rows_spiked = [self._row(24500 + i * STEP, gamma=0.001) for i in range(7)]
        rows_spiked[3].ce_gamma = 0.02   # center strike gamma spike
        calm_result = analyze_oi_cluster(rows_calm, 24650, "CE", STEP)
        spiked_result = analyze_oi_cluster(rows_spiked, 24650, "CE", STEP)
        assert spiked_result["gamma_instability"] is not None
        assert spiked_result["fake_index"] >= calm_result["fake_index"]

    def test_theta_defense_feeds_wall_score(self):
        rows_flat = [self._row(24500 + i * STEP, theta=-5.0) for i in range(7)]
        rows_decay = [self._row(24500 + i * STEP, theta=-5.0) for i in range(7)]
        rows_decay[3].ce_theta = -40.0   # much faster decay at center strike
        flat_result = analyze_oi_cluster(rows_flat, 24650, "CE", STEP)
        decay_result = analyze_oi_cluster(rows_decay, 24650, "CE", STEP)
        assert decay_result["theta_defense_score"] is not None
        assert decay_result["institutional_wall_score"] >= flat_result["institutional_wall_score"]

    def test_missing_greeks_degrade_gracefully_not_raise(self):
        rows = [StrikeRow(strike=24500 + i * STEP, ce_oi=100000, ce_vol=5000) for i in range(7)]
        result = analyze_oi_cluster(rows, 24650, "CE", STEP)
        assert result["tradeable_data"] is True
        assert result["data_completeness"] < 1.0

    def test_factor_weights_scale_components(self):
        rows = [self._row(24500 + i * STEP) for i in range(7)]
        rows[3].ce_vol = 50000
        neutral = analyze_oi_cluster(rows, 24650, "CE", STEP)
        boosted = analyze_oi_cluster(rows, 24650, "CE", STEP, factor_weights={"volume": 1.5})
        assert boosted["institutional_wall_score"] >= neutral["institutional_wall_score"]


class TestVolatilityRegime:
    def test_high_volatility_from_large_atr_pct(self):
        assert classify_volatility({"atr_14": 200}, 24000) == "HIGH"

    def test_low_volatility_from_small_atr_pct(self):
        assert classify_volatility({"atr_14": 20}, 24000) == "LOW"

    def test_unknown_without_data(self):
        assert classify_volatility(None, 24000) == "UNKNOWN"
        assert classify_volatility({"atr_14": 100}, None) == "UNKNOWN"

    def test_regime_weights_label_reflects_volatility(self):
        w = v3_regime_weights({"regime": "TRENDING", "atr_14": 200}, 24000)
        assert "VOLATILE" in w["label"]
        calm = v3_regime_weights({"regime": "RANGING", "atr_14": 20}, 24000)
        assert "CALM" in calm["label"]

    def test_regime_weights_still_bounded_with_volatility(self):
        w = v3_regime_weights({"regime": "TRENDING", "atr_14": 99999}, 24000)
        assert 0.6 <= w["break_mult"] <= 1.6
        assert 0.6 <= w["hold_mult"] <= 1.6


class TestWallMigration:
    def test_center_of_mass_weighted_by_oi(self):
        rows = [StrikeRow(strike=24500, ce_oi=100), StrikeRow(strike=24550, ce_oi=900)]
        center = compute_oi_cluster_center(rows, 24550, "CE", STEP)
        # heavily weighted toward 24550 (900 of 1000 total OI)
        assert 24540 < center < 24550

    def test_none_center_strike_returns_none(self):
        assert compute_oi_cluster_center([], None, "CE", STEP) is None

    def test_migration_up_detected(self):
        result = detect_wall_migration([24600, 24610], 24700, STEP, min_shift_steps=0.5)
        assert result["migrating"] == "UP"

    def test_migration_down_detected(self):
        result = detect_wall_migration([24700, 24690], 24600, STEP, min_shift_steps=0.5)
        assert result["migrating"] == "DOWN"

    def test_stable_within_threshold(self):
        result = detect_wall_migration([24650], 24660, STEP, min_shift_steps=0.5)
        assert result["migrating"] == "STABLE"

    def test_missing_data_returns_none(self):
        assert detect_wall_migration([], 24650, STEP) == {"migrating": None, "shift_strikes": None}
        assert detect_wall_migration([24650], None, STEP) == {"migrating": None, "shift_strikes": None}
        assert detect_wall_migration(None, 24650, STEP) == {"migrating": None, "shift_strikes": None}


class TestLearnAdaptiveWeights:
    def _trade(self, exit_reason, **factors):
        return {"exit_reason": exit_reason, "factors": factors}

    def test_too_few_trades_leaves_weights_unchanged(self):
        trades = [self._trade("TARGET HIT", volume=80) for _ in range(5)]
        weights, diag = learn_adaptive_weights(trades, min_sample=20)
        assert weights == V3_DEFAULT_FACTOR_WEIGHTS
        assert diag["adjusted"] is False

    def test_factor_that_predicts_wins_gets_nudged_up(self):
        trades = []
        for _ in range(15):
            trades.append(self._trade("TARGET HIT", volume=90))
        for _ in range(15):
            trades.append(self._trade("STOP LOSS", volume=10))
        weights, diag = learn_adaptive_weights(trades, min_sample=20, step=0.05)
        assert diag["adjusted"] is True
        assert weights["volume"] > V3_DEFAULT_FACTOR_WEIGHTS["volume"]

    def test_factor_that_predicts_losses_gets_nudged_down(self):
        trades = []
        for _ in range(15):
            trades.append(self._trade("STOP LOSS", volume=90))
        for _ in range(15):
            trades.append(self._trade("TARGET HIT", volume=10))
        weights, diag = learn_adaptive_weights(trades, min_sample=20, step=0.05)
        assert weights["volume"] < V3_DEFAULT_FACTOR_WEIGHTS["volume"]

    def test_weights_never_exceed_bounds_across_many_nudges(self):
        weights = None
        for _ in range(50):
            trades = [self._trade("TARGET HIT", volume=90) for _ in range(15)] + \
                     [self._trade("STOP LOSS", volume=10) for _ in range(15)]
            weights, _ = learn_adaptive_weights(trades, current_weights=weights, min_sample=20, step=0.1)
        assert weights["volume"] <= 1.5

    def test_time_exits_excluded_from_resolved_sample(self):
        trades = [self._trade("TIME EXIT", volume=50) for _ in range(50)]
        weights, diag = learn_adaptive_weights(trades, min_sample=20)
        assert diag["adjusted"] is False
        assert diag["sample_size"] == 0

    def test_never_raises_on_missing_factor_data(self):
        trades = [{"exit_reason": "TARGET HIT"} for _ in range(30)]
        weights, diag = learn_adaptive_weights(trades, min_sample=20)
        assert weights == V3_DEFAULT_FACTOR_WEIGHTS
