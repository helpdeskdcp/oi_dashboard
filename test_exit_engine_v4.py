"""
test_exit_engine_v4.py -- regression tests for exit_engine_v4.py (Institutional
Exit Engine V4: dynamic ATR/structure trailing stop, structure-break/opposite-
signal/VWAP/momentum-fade exits, adaptive ATR+structure targets, adaptive
30-60min time exit, Trade Quality Score, predictive confidence).

Synthetic data only -- no live broker/market dependency, same philosophy as
test_sr_engine_v3.py. The breakout fixtures below (_bullish_breakout_candles/
_bearish_breakout_candles) are tuned to reliably clear dynamic_sr_engine.
evaluate()'s OWN unchanged gates (23+ candle trend window, single decisive
breakout candle, confidence>=60, outside the 10:00-10:59 blackout) -- the
opposite-signal tests exercise the REAL entry engine, not a mock, since
check_opposite_signal's entire job is correctly wiring into it.
"""
import datetime as dt

import pytest

from oi_engine import StrikeRow
import dynamic_sr_engine as dsr
import exit_engine_v4 as v4


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _candles_from_prices(prices, start_dt, volume=0):
    candles = []
    t = start_dt
    for i in range(1, len(prices)):
        o, c = prices[i - 1], prices[i]
        h = max(o, c) + abs(c - o) * 0.05 + 0.5
        l = min(o, c) - abs(c - o) * 0.05 - 0.5
        candles.append({"datetime": t, "open": o, "high": h, "low": l, "close": c, "volume": volume})
        t += dt.timedelta(minutes=3)
    return candles


def _flat_candles(n=30, price=24000.0, start_dt=dt.datetime(2026, 1, 5, 11, 15), volume=0):
    """Flat/no-trend candles -- long enough for calc_atr (needs 15) and
    detect_trend (needs 23); short of anything dynamic_sr_engine.evaluate()
    would call a breakout, so opposite-signal checks stay False by default."""
    prices = [price] * (n + 1)
    return _candles_from_prices(prices, start_dt, volume)


def _bullish_breakout_candles(start_dt=dt.datetime(2026, 1, 5, 11, 15), volume=0):
    """24 candles creeping 24000->24023, then a decisive +50 breakout candle
    through pdh=24040. Empirically verified: dynamic_sr_engine.evaluate()
    returns a BUY signal at confidence ~67.3 (well clear of the 60 gate)."""
    prices = [24000.0 + i for i in range(24)] + [24073.0]
    return _candles_from_prices(prices, start_dt, volume), 24040, 23900


def _bearish_breakout_candles(start_dt=dt.datetime(2026, 1, 5, 11, 15), volume=0):
    """24 candles creeping 24100->24077, then a decisive -60 breakdown
    candle through pdl(equivalent)=24060. Empirically verified: SELL signal
    at confidence ~71.5."""
    prices = [24100.0 - i for i in range(24)] + [24017.0]
    return _candles_from_prices(prices, start_dt, volume), 24140, 24060


def _rising_volume_day(n=20, start_price=24000.0, step=2.0,
                        start_dt=dt.datetime(2026, 1, 5, 9, 15), volume=1000):
    """Steadily rising candles WITH a real volume feed, all on the same
    calendar day -- needed for calc_vwap (returns None without volume) and
    volume-based momentum checks."""
    prices = [start_price + i * step for i in range(n + 1)]
    return _candles_from_prices(prices, start_dt, volume)


def _flat_rows(low=24500, high=24950, step=50):
    rows = []
    for k in range(low, high, step):
        r = StrikeRow(strike=k)
        r.ce_oi = r.pe_oi = 100000
        r.ce_delta, r.pe_delta = 0.5, -0.5
        rows.append(r)
    return rows


def _oi_cycle(atm=24700, rows=None, ts="2026-01-05T11:15:00"):
    return {"cycle": {"atm": atm, "ts": ts}, "rows": rows if rows is not None else _flat_rows()}


# ---------------------------------------------------------------------------
# 1. Trailing stop
# ---------------------------------------------------------------------------

class TestUpdateTrailingStop:
    def test_buy_trails_up_with_atr(self):
        sl = v4.update_trailing_stop("BUY", current_sl=23950, current_price=24100, atr=20,
                                      resistances=[], supports=[])
        assert sl == 24100 - v4.ATR_TRAIL_MULT * 20
        assert sl > 23950

    def test_buy_never_loosens(self):
        # ATR candidate (24070) is BELOW the existing SL (24080) -- must not move down.
        sl = v4.update_trailing_stop("BUY", current_sl=24080, current_price=24100, atr=20,
                                      resistances=[], supports=[])
        assert sl == 24080

    def test_buy_prefers_more_protective_of_atr_and_structure(self):
        # Structure rung (24090) sits closer to price than the ATR candidate (24070) -> wins.
        sl = v4.update_trailing_stop("BUY", current_sl=24000, current_price=24100, atr=20,
                                      resistances=[], supports=[24090, 23800])
        assert sl == 24090

    def test_sell_trails_down_with_atr_and_never_loosens(self):
        sl = v4.update_trailing_stop("SELL", current_sl=24150, current_price=24000, atr=20,
                                      resistances=[], supports=[])
        assert sl == 24000 + v4.ATR_TRAIL_MULT * 20
        assert sl < 24150
        sl2 = v4.update_trailing_stop("SELL", current_sl=24020, current_price=24000, atr=20,
                                       resistances=[], supports=[])
        assert sl2 == 24020   # ATR candidate (24030) is less protective -- SL must not loosen to it

    def test_no_atr_no_structure_leaves_sl_unchanged(self):
        sl = v4.update_trailing_stop("BUY", current_sl=23950, current_price=24100, atr=None,
                                      resistances=[], supports=[])
        assert sl == 23950


# ---------------------------------------------------------------------------
# 2. Structure-break exit
# ---------------------------------------------------------------------------

class TestStructureBreak:
    def test_buy_exits_when_close_falls_back_below_originating_level(self):
        assert v4.check_structure_break("BUY", originating_level=24040, close_price=24030) is True

    def test_buy_holds_while_close_stays_above_level(self):
        assert v4.check_structure_break("BUY", originating_level=24040, close_price=24060) is False

    def test_sell_exits_when_close_rises_back_above_originating_level(self):
        assert v4.check_structure_break("SELL", originating_level=24040, close_price=24050) is True

    def test_sell_holds_while_close_stays_below_level(self):
        assert v4.check_structure_break("SELL", originating_level=24040, close_price=24020) is False

    def test_none_level_never_triggers(self):
        assert v4.check_structure_break("BUY", originating_level=None, close_price=20000) is False


# ---------------------------------------------------------------------------
# 3. Opposite-signal exit (exercises the REAL, unchanged entry engine)
# ---------------------------------------------------------------------------

class TestOppositeSignal:
    def test_no_signal_on_flat_data_does_not_exit(self):
        candles = _flat_candles()
        assert v4.check_opposite_signal("BUY", candles, 24100, 23900, candles[-1]["close"]) is False

    def test_same_direction_signal_is_not_opposite(self):
        candles, pdh, pdl = _bullish_breakout_candles()
        result = dsr.evaluate(candles, pdh, pdl, current_price=candles[-1]["close"])
        assert result["signal"]["direction"] == "BUY"
        assert v4.check_opposite_signal("BUY", candles, pdh, pdl, candles[-1]["close"]) is False

    def test_genuine_opposite_signal_triggers_exit_for_buy_position(self):
        candles, pdh, pdl = _bearish_breakout_candles()
        result = dsr.evaluate(candles, pdh, pdl, current_price=candles[-1]["close"])
        assert result["signal"]["direction"] == "SELL"   # sanity: fixture really produces the opposite side
        assert v4.check_opposite_signal("BUY", candles, pdh, pdl, candles[-1]["close"]) is True

    def test_genuine_opposite_signal_triggers_exit_for_sell_position(self):
        candles, pdh, pdl = _bullish_breakout_candles()
        assert v4.check_opposite_signal("SELL", candles, pdh, pdl, candles[-1]["close"]) is True


# ---------------------------------------------------------------------------
# 4. VWAP-cross exit
# ---------------------------------------------------------------------------

class TestVwapCross:
    def test_vwap_none_never_exits(self):
        # No volume feed -> calc_vwap returns None for every pure index -- must be a pure no-op.
        assert v4.check_vwap_cross("BUY", close_price=23000, vwap=None, atr=20) is False

    def test_buy_exits_when_decisively_below_vwap(self):
        assert v4.check_vwap_cross("BUY", close_price=23900, vwap=24000, atr=20) is True

    def test_buy_holds_within_the_atr_buffer(self):
        # 1 point below VWAP, well inside 0.15*ATR(20)=3pt buffer -- a token wick, not a real cross.
        assert v4.check_vwap_cross("BUY", close_price=23999, vwap=24000, atr=20) is False

    def test_sell_exits_when_decisively_above_vwap(self):
        assert v4.check_vwap_cross("SELL", close_price=24100, vwap=24000, atr=20) is True

    def test_sell_holds_within_the_atr_buffer(self):
        assert v4.check_vwap_cross("SELL", close_price=24001, vwap=24000, atr=20) is False


# ---------------------------------------------------------------------------
# 5. Momentum-fade exit
# ---------------------------------------------------------------------------

class TestMomentumFade:
    def test_score_uses_roc_only_when_no_volume_or_oi_data(self):
        candles = _flat_candles(volume=0)   # flat -> roc==0 -> neutral 50
        score = v4.compute_momentum_score("BUY", candles, entry_oi_wall=None, current_oi_wall=None)
        assert score == 50.0

    def test_declining_price_fades_momentum_for_buy_direction(self):
        candles = _candles_from_prices([24100.0 - i * 3 for i in range(31)], dt.datetime(2026, 1, 5, 11, 15))
        score = v4.compute_momentum_score("BUY", candles, entry_oi_wall=None, current_oi_wall=None)
        assert score < 50.0
        assert v4.check_momentum_fade(score) is True

    def test_rising_price_does_not_fade_momentum_for_buy_direction(self):
        candles = _candles_from_prices([24000.0 + i * 3 for i in range(31)], dt.datetime(2026, 1, 5, 11, 15))
        score = v4.compute_momentum_score("BUY", candles, entry_oi_wall=None, current_oi_wall=None)
        assert v4.check_momentum_fade(score) is False

    def test_volume_drop_lowers_score_when_feed_present(self):
        candles = _flat_candles(volume=1000)
        candles[-1]["volume"] = 50   # sharp drop below the rolling average
        low_vol_score = v4.compute_momentum_score("BUY", candles, entry_oi_wall=None, current_oi_wall=None)
        candles[-1]["volume"] = 1000
        normal_score = v4.compute_momentum_score("BUY", candles, entry_oi_wall=None, current_oi_wall=None)
        assert low_vol_score < normal_score

    def test_weakening_oi_wall_lowers_score(self):
        candles = _flat_candles()
        weakened = v4.compute_momentum_score("BUY", candles, entry_oi_wall=80.0, current_oi_wall=20.0)
        stable = v4.compute_momentum_score("BUY", candles, entry_oi_wall=80.0, current_oi_wall=80.0)
        assert weakened < stable

    def test_threshold_boundary(self):
        assert v4.check_momentum_fade(v4.MOMENTUM_FADE_THRESHOLD - 0.1) is True
        assert v4.check_momentum_fade(v4.MOMENTUM_FADE_THRESHOLD + 0.1) is False


# ---------------------------------------------------------------------------
# 6. Adaptive ATR/structure targets
# ---------------------------------------------------------------------------

class TestAdaptiveTargets:
    def test_buy_targets_strictly_increasing(self):
        targets = v4.compute_adaptive_targets("BUY", entry=24000, atr=20, range1=70,
                                               resistances=[24100, 24300, 24600], supports=[])
        assert len(targets) == 3
        assert targets[0] < targets[1] < targets[2]
        assert all(t > 24000 for t in targets)

    def test_sell_targets_strictly_decreasing(self):
        targets = v4.compute_adaptive_targets("SELL", entry=24000, atr=20, range1=70,
                                               resistances=[], supports=[23900, 23700, 23400])
        assert targets[0] > targets[1] > targets[2]
        assert all(t < 24000 for t in targets)

    def test_prefers_closer_ladder_rung_over_farther_atr_projection(self):
        # ATR(30)*1.5=45 would project T1 to 24045; a rung sits much closer at 24010.
        targets = v4.compute_adaptive_targets("BUY", entry=24000, atr=30, range1=70,
                                               resistances=[24010, 24300, 24600], supports=[])
        assert targets[0] == 24010

    def test_falls_back_to_range1_when_atr_unavailable(self):
        targets = v4.compute_adaptive_targets("BUY", entry=24000, atr=None, range1=70,
                                               resistances=[], supports=[])
        assert targets[0] == round(24000 + 1.5 * 70, 2)


# ---------------------------------------------------------------------------
# 7. Adaptive max-hold
# ---------------------------------------------------------------------------

class TestAdaptiveMaxHold:
    def test_defaults_to_base_without_price(self):
        assert v4.compute_adaptive_max_hold(atr=20, current_price=None, adx=30) == v4.ADAPTIVE_HOLD_BASE_MINUTES

    def test_extends_toward_max_when_calm_and_trending(self):
        # atr_pct = 10/24000 ~= 0.04% (calm), adx=40 (strongly trending)
        hold = v4.compute_adaptive_max_hold(atr=10, current_price=24000, adx=40)
        assert hold > v4.ADAPTIVE_HOLD_BASE_MINUTES
        assert hold <= v4.ADAPTIVE_HOLD_MAX_MINUTES

    def test_stays_near_base_when_volatile_and_choppy(self):
        # atr_pct = 60/24000 = 0.25% (well past the "wild" edge), adx=10 (no trend)
        hold = v4.compute_adaptive_max_hold(atr=60, current_price=24000, adx=10)
        assert hold == v4.ADAPTIVE_HOLD_BASE_MINUTES

    def test_stays_within_bounds_across_a_range_of_inputs(self):
        for atr in (5, 20, 50, 100):
            for adx in (None, 5, 20, 45):
                hold = v4.compute_adaptive_max_hold(atr=atr, current_price=24000, adx=adx)
                assert v4.ADAPTIVE_HOLD_BASE_MINUTES <= hold <= v4.ADAPTIVE_HOLD_MAX_MINUTES


# ---------------------------------------------------------------------------
# Trade Quality Score
# ---------------------------------------------------------------------------

class TestTradeQualityScore:
    def test_missing_data_defaults_to_neutral_components(self):
        candles = _flat_candles(volume=0)
        quality = v4.compute_trade_quality_score(
            "BUY", entry=24000, level_price=23980, atr=20, candles=candles,
            pdh=24100, pdl=23900, vwap=None, oi_wall_score=None, delta_score=None,
        )
        assert quality["vwap_alignment"] == 50.0
        assert quality["oi_wall_strength"] == 50.0
        assert quality["delta_confirmation"] == 50.0
        assert 0 <= quality["composite"] <= 100

    def test_composite_always_in_valid_range(self):
        candles = _bullish_breakout_candles()[0]
        quality = v4.compute_trade_quality_score(
            "BUY", entry=24073, level_price=24040, atr=15, candles=candles,
            pdh=24040, pdl=23900, vwap=24000, oi_wall_score=80, delta_score=90,
        )
        assert 0 <= quality["composite"] <= 100
        for key in v4.QUALITY_WEIGHTS:
            assert 0 <= quality[key] <= 100

    def test_oi_and_delta_scores_pass_through(self):
        candles = _flat_candles()
        quality = v4.compute_trade_quality_score(
            "BUY", entry=24000, level_price=23980, atr=20, candles=candles,
            pdh=24100, pdl=23900, oi_wall_score=77.0, delta_score=88.0,
        )
        assert quality["oi_wall_strength"] == 77.0
        assert quality["delta_confirmation"] == 88.0


# ---------------------------------------------------------------------------
# Predictive confidence
# ---------------------------------------------------------------------------

class TestPredictiveConfidence:
    def test_bounded_within_target_band(self):
        for quality in (0, 25, 50, 75, 100):
            for adx in (None, 10, 25, 45):
                conf = v4.compute_predictive_confidence(quality, breakout_dist=30, atr=20, adx=adx)
                assert 40 <= conf <= 95

    def test_spreads_across_a_meaningful_range(self):
        low = v4.compute_predictive_confidence(quality_composite=20, breakout_dist=5, atr=20, adx=8)
        high = v4.compute_predictive_confidence(quality_composite=95, breakout_dist=80, atr=20, adx=45)
        assert high - low > 25   # not clustered like dynamic_sr_engine's entry-gate confidence (66-70 band)

    def test_higher_quality_yields_higher_confidence_all_else_equal(self):
        low = v4.compute_predictive_confidence(quality_composite=30, breakout_dist=30, atr=20, adx=25)
        high = v4.compute_predictive_confidence(quality_composite=90, breakout_dist=30, atr=20, adx=25)
        assert high > low


# ---------------------------------------------------------------------------
# OI context indexing
# ---------------------------------------------------------------------------

class TestOiIndex:
    def _cycles(self):
        return [
            {"cycle": {"ts": "2026-01-05T09:15:00", "atm": 24000}, "rows": []},
            {"cycle": {"ts": "2026-01-05T09:20:00", "atm": 24010}, "rows": []},
            {"cycle": {"ts": "2026-01-05T09:30:00", "atm": 24020}, "rows": []},
        ]

    def test_nearest_within_tolerance(self):
        idx = v4.build_oi_index(self._cycles())
        found = v4.nearest_oi_context(idx, dt.datetime(2026, 1, 5, 9, 21), tolerance_minutes=3)
        assert found["cycle"]["atm"] == 24010

    def test_outside_tolerance_returns_none(self):
        idx = v4.build_oi_index(self._cycles())
        found = v4.nearest_oi_context(idx, dt.datetime(2026, 1, 5, 10, 0), tolerance_minutes=3)
        assert found is None

    def test_empty_cycles_returns_none(self):
        idx = v4.build_oi_index([])
        assert idx == ()
        assert v4.nearest_oi_context(idx, dt.datetime(2026, 1, 5, 9, 21)) is None


# ---------------------------------------------------------------------------
# open_position
# ---------------------------------------------------------------------------

class TestOpenPosition:
    def test_builds_a_complete_position_from_a_real_signal(self):
        candles, pdh, pdl = _bullish_breakout_candles()
        result = dsr.evaluate(candles, pdh, pdl, current_price=candles[-1]["close"])
        sig = result["signal"]
        assert sig is not None

        pos = v4.open_position(sig, candles[-1]["datetime"], candles, pdh, pdl)
        assert pos["direction"] == "BUY"
        assert pos["entry"] == sig["entry"]
        assert pos["current_sl"] == sig["stop_loss"]
        assert len(pos["targets"]) == 3
        assert pos["targets"][0] < pos["targets"][1] < pos["targets"][2]
        assert v4.ADAPTIVE_HOLD_BASE_MINUTES <= pos["max_hold_minutes"] <= v4.ADAPTIVE_HOLD_MAX_MINUTES
        assert 0 <= pos["quality_score"] <= 100
        assert 40 <= pos["predictive_confidence"] <= 95

    def test_entry_gate_confidence_is_untouched(self):
        # open_position must carry the ORIGINAL entry-gate confidence through
        # unchanged (dynamic_sr_engine.compute_confidence is explicitly out of
        # scope for V4) -- not overwrite it with predictive_confidence.
        candles, pdh, pdl = _bullish_breakout_candles()
        sig = dsr.evaluate(candles, pdh, pdl, current_price=candles[-1]["close"])["signal"]
        pos = v4.open_position(sig, candles[-1]["datetime"], candles, pdh, pdl)
        assert pos["confidence"] == sig["confidence"]

    def test_max_sl_atr_mult_caps_a_too_wide_stop(self):
        candles, pdh, pdl = _bullish_breakout_candles()
        sig = dsr.evaluate(candles, pdh, pdl, current_price=candles[-1]["close"])["signal"]
        uncapped = v4.open_position(sig, candles[-1]["datetime"], candles, pdh, pdl)
        raw_dist = uncapped["entry"] - uncapped["current_sl"]
        atr = uncapped["entry_atr"]
        tight_mult = (raw_dist / atr) / 2   # half the raw distance -- guaranteed to bind

        capped = v4.open_position(sig, candles[-1]["datetime"], candles, pdh, pdl,
                                   max_sl_atr_mult=tight_mult)
        assert capped["current_sl"] == pytest.approx(capped["entry"] - tight_mult * atr)
        assert capped["current_sl"] == capped["initial_sl"]
        assert capped["current_sl"] > uncapped["current_sl"]   # moved closer to entry, less risk

    def test_max_sl_atr_mult_never_loosens_a_tighter_structural_stop(self):
        # A cap wider than the raw structural distance must be a no-op --
        # max_sl_atr_mult only ever tightens, never widens, the SL.
        candles, pdh, pdl = _bullish_breakout_candles()
        sig = dsr.evaluate(candles, pdh, pdl, current_price=candles[-1]["close"])["signal"]
        uncapped = v4.open_position(sig, candles[-1]["datetime"], candles, pdh, pdl)

        capped = v4.open_position(sig, candles[-1]["datetime"], candles, pdh, pdl,
                                   max_sl_atr_mult=100.0)
        assert capped["current_sl"] == uncapped["current_sl"]

    def test_max_sl_atr_mult_none_preserves_todays_exact_behavior(self):
        candles, pdh, pdl = _bullish_breakout_candles()
        sig = dsr.evaluate(candles, pdh, pdl, current_price=candles[-1]["close"])["signal"]
        pos = v4.open_position(sig, candles[-1]["datetime"], candles, pdh, pdl, max_sl_atr_mult=None)
        assert pos["current_sl"] == sig["stop_loss"]


# ---------------------------------------------------------------------------
# manage_exit -- integration + priority ordering
# ---------------------------------------------------------------------------

def _base_position(direction="BUY", entry=24000.0, entry_time=None, current_sl=None,
                    targets=None, originating_level=None, entry_atr=20.0):
    return {
        "direction": direction, "entry": entry,
        "entry_time": entry_time or dt.datetime(2026, 1, 5, 11, 15),
        "current_sl": current_sl if current_sl is not None else (23900.0 if direction == "BUY" else 24100.0),
        "initial_sl": current_sl if current_sl is not None else (23900.0 if direction == "BUY" else 24100.0),
        "originating_level": originating_level,
        "targets": targets if targets is not None else ([25000.0, 26000.0, 27000.0] if direction == "BUY"
                                                          else [23000.0, 22000.0, 21000.0]),
        "best_target_hit": None,
        "entry_atr": entry_atr, "entry_adx": None,
        "entry_oi_wall_score": None, "entry_delta_score": None,
        "max_hold_minutes": 30, "confidence": 65.0,
        "quality_score": 60.0, "quality_breakdown": {}, "predictive_confidence": 65.0,
    }


class TestManageExitStopLossAndTarget:
    def test_stop_loss_hit_intrabar(self):
        candles = _flat_candles(price=24000)
        pos = _base_position(current_sl=23990.0)
        candle = {**candles[-1], "low": 23985.0, "high": 24005.0, "close": 23995.0}
        result = v4.manage_exit(pos, candle, candles, 24100, 23900, today=dt.date(2026, 1, 5))
        assert result["exit"] is True
        assert result["exit_reason"] == "STOP LOSS"
        assert result["exit_price"] == pos["current_sl"]

    def test_final_target_hit_intrabar(self):
        candles = _flat_candles(price=24000)
        pos = _base_position(targets=[24050.0, 24100.0, 24150.0])
        candle = {**candles[-1], "low": 24140.0, "high": 24160.0, "close": 24150.0}
        result = v4.manage_exit(pos, candle, candles, 24200, 23900, today=dt.date(2026, 1, 5))
        assert result["exit"] is True
        assert result["exit_reason"] == "TARGET HIT"
        assert result["exit_price"] == 24150.0

    def test_partial_target_hit_does_not_exit_but_trails_sl(self):
        candles = _flat_candles(price=24000)
        pos = _base_position(targets=[24050.0, 24100.0, 24150.0], entry_time=candles[-1]["datetime"] - dt.timedelta(minutes=5))
        candle = {**candles[-1], "low": 24045.0, "high": 24055.0, "close": 24050.0}
        result = v4.manage_exit(pos, candle, candles, 24200, 23900, today=dt.date(2026, 1, 5))
        assert result["exit"] is False
        # SL trails to at least breakeven after T1 -- possibly further still via
        # the ATR/structure ratchet applied afterward, whichever is MORE protective.
        assert result["position"]["current_sl"] >= pos["entry"]

    def test_pullback_candle_does_not_loosen_previously_ratcheted_sl(self):
        """Regression: a candle whose high only re-clears a NEARER target
        than one already reached in an earlier candle must not downgrade
        best_target_hit/current_sl -- the ratchet only ever advances."""
        candles = _flat_candles(price=24000)
        pos = _base_position(targets=[24050.0, 24100.0, 24150.0],
                              entry_time=candles[-1]["datetime"] - dt.timedelta(minutes=10))

        candle1 = {**candles[-1], "low": 24040.0, "high": 24110.0, "close": 24080.0,
                   "datetime": candles[-1]["datetime"] - dt.timedelta(minutes=5)}
        result1 = v4.manage_exit(pos, candle1, candles, 24200, 23900, today=dt.date(2026, 1, 5))
        assert result1["exit"] is False
        assert pos["best_target_hit"] == 1
        assert pos["current_sl"] >= 24050.0
        sl_after_candle1 = pos["current_sl"]

        # Stays above sl_after_candle1 (no stop-out) but its high only re-clears
        # the nearer target (t0=24050), not t1=24100 -- the pullback scenario.
        candle2 = {**candles[-1], "low": sl_after_candle1 + 5.0, "high": 24095.0, "close": 24090.0}
        result2 = v4.manage_exit(pos, candle2, candles, 24200, 23900, today=dt.date(2026, 1, 5))
        assert result2["exit"] is False
        assert pos["best_target_hit"] == 1
        assert pos["current_sl"] >= sl_after_candle1


class TestManageExitStructureAndSignalExits:
    def test_structure_break_exit(self):
        candles = _flat_candles(price=24000)
        pos = _base_position(originating_level=24040.0)
        candle = {**candles[-1], "close": 24020.0, "low": 24015.0, "high": 24025.0}
        result = v4.manage_exit(pos, candle, candles, 24100, 23900, today=dt.date(2026, 1, 5))
        assert result["exit"] is True
        assert result["exit_reason"] == "STRUCTURE BREAK"

    def test_opposite_signal_exit(self):
        candles, pdh, pdl = _bearish_breakout_candles()
        pos = _base_position(direction="BUY", entry=24200.0, current_sl=20000.0,
                              targets=[30000.0, 31000.0, 32000.0], originating_level=10000.0)
        last = candles[-1]
        result = v4.manage_exit(pos, last, candles, pdh, pdl, today=last["datetime"].date())
        assert result["exit"] is True
        assert result["exit_reason"] == "OPPOSITE SIGNAL"

    def test_stop_loss_takes_priority_over_structure_break(self):
        # This candle satisfies BOTH conditions -- SL is checked first, so it must win.
        candles = _flat_candles(price=24000)
        pos = _base_position(current_sl=23990.0, originating_level=24040.0)
        candle = {**candles[-1], "close": 24020.0, "low": 23985.0, "high": 24025.0}
        result = v4.manage_exit(pos, candle, candles, 24100, 23900, today=dt.date(2026, 1, 5))
        assert result["exit_reason"] == "STOP LOSS"


class TestManageExitVwapAndMomentum:
    def test_vwap_cross_exit(self):
        candles = _rising_volume_day(n=20, start_price=24000, step=2)
        pos = _base_position(direction="BUY", entry=24020.0, current_sl=23000.0,
                              targets=[26000.0, 27000.0, 28000.0], entry_atr=5.0)
        crash = {**candles[-1], "close": candles[0]["close"] - 200, "low": candles[0]["close"] - 210,
                 "high": candles[0]["close"] - 190}
        result = v4.manage_exit(pos, crash, candles, 25000, 23000, today=candles[-1]["datetime"].date())
        assert result["exit"] is True
        assert result["exit_reason"] == "VWAP CROSS"

    def test_momentum_fade_exit(self):
        candles = _candles_from_prices([24100.0 - i * 3 for i in range(31)], dt.datetime(2026, 1, 5, 11, 15))
        pos = _base_position(direction="BUY", entry=24100.0, current_sl=20000.0,
                              targets=[30000.0, 31000.0, 32000.0], originating_level=10000.0)
        last = candles[-1]
        # pdh/pdl chosen well away from this price path so no structural rung is crossed
        result = v4.manage_exit(pos, last, candles, 30000, 29000, today=last["datetime"].date())
        assert result["exit"] is True
        assert result["exit_reason"] == "MOMENTUM FADE"


class TestManageExitTimeFallback:
    def test_time_exit_only_after_max_hold_with_nothing_else_triggered(self):
        candles = _flat_candles(price=24000)
        entry_time = candles[-1]["datetime"] - dt.timedelta(minutes=35)
        pos = _base_position(entry_time=entry_time)
        pos["max_hold_minutes"] = 30
        result = v4.manage_exit(pos, candles[-1], candles, 24100, 23900, today=dt.date(2026, 1, 5))
        assert result["exit"] is True
        assert result["exit_reason"] == "TIME EXIT"

    def test_no_exit_before_max_hold_elapses(self):
        candles = _flat_candles(price=24000)
        entry_time = candles[-1]["datetime"] - dt.timedelta(minutes=10)
        pos = _base_position(entry_time=entry_time)
        pos["max_hold_minutes"] = 30
        result = v4.manage_exit(pos, candles[-1], candles, 24100, 23900, today=dt.date(2026, 1, 5))
        assert result["exit"] is False
