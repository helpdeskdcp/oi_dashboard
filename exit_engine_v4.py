#!/usr/bin/env python3
"""
exit_engine_v4.py -- Institutional Exit Engine (V4).

Manages EXIT decisions for positions opened by dynamic_sr_engine.py's (V1)
entry signal -- entry detection itself is explicitly OUT OF SCOPE and left
byte-for-byte unchanged (see project notes: a prior attempt to also delay
entry by one candle for "confirmation" was backtested and reverted --
average win size fell because it ate into the fixed 30min hold window's
favorable-drift capture, which is where nearly all of V1's realized profit
came from). This module owns everything that happens AFTER entry:

  1. Dynamic ATR + market-structure trailing stop (never loosens).
  2. Immediate exit if price closes back through the ORIGINATING level.
  3. Immediate exit on a validated opposite-direction signal (re-running
     dynamic_sr_engine.evaluate() unchanged -- same entry-quality bar).
  4. Immediate exit if price closes decisively across VWAP against the trade.
  5. Exit when momentum fades (volume, ROC, OI-wall strength, delta).
  6. Adaptive ATR/structure-based profit targets (replacing V1's fixed
     range1-multiple ladder, which V3 backtesting found was hit in only
     ~2% of trades -- almost never reachable inside any realistic hold).
  7. Adaptive (30-60min) time exit -- last-resort fallback only, checked
     after every other condition above.

Pure, side-effect-free (same contract as oi_engine.py/sr_engine_v3.py -- no
network/DB calls; callers pass in whatever candles/OI context they already
have). Every OI-wall / delta / VWAP / volume check degrades to a neutral
score or a no-op when its underlying data isn't available -- pure indices
(NIFTY/BANKNIFTY/SENSEX/FINNIFTY) have no tradable-volume feed so VWAP is
always None for them (see market_structure.calc_vwap), and a backtest
without an OI cycle joined to the candle timestamp has no wall/delta data --
same "not applicable != violated" convention dynamic_sr_engine.py's
volume_confirmed gate already uses. This is intentional, not a shortcut:
scoring an unavailable factor as a failure would silently punish every
trade on a pure index for a data gap that has nothing to do with trade
quality.
"""

import bisect
import datetime as dt

from market_structure import calc_atr, calc_adx, calc_vwap
from dynamic_sr_engine import (
    evaluate as evaluate_dynamic_sr, build_levels, extend_levels,
    detect_trend, momentum_roc, avg_volume,
)
from sr_engine_v3 import analyze_oi_cluster
from position_sizing import compute_quantity


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


# ---------------------------------------------------------------------------
# OI context indexing (nearest oi_history.db cycle to a candle timestamp)
# ---------------------------------------------------------------------------

def build_oi_index(cycles: list):
    """cycles: backtest.load_cycles(...) output (already sorted ASC by ts).
    Returns an opaque index for nearest_oi_context(); () if cycles is empty
    so callers can treat "no OI data joined" and "no OI data at all" the
    same way (both produce None from nearest_oi_context)."""
    if not cycles:
        return ()
    ts_list = [dt.datetime.fromisoformat(c["cycle"]["ts"]) for c in cycles]
    return (ts_list, cycles)


def nearest_oi_context(oi_index, target_dt: dt.datetime, tolerance_minutes: float = 3.0):
    """Nearest OI cycle to target_dt, or None if there's no cycle within
    tolerance (candle archive and OI-poll cycles run on independent,
    unaligned clocks -- see module docstring on graceful degradation)."""
    if not oi_index or target_dt is None:
        return None
    ts_list, cycles = oi_index
    idx = bisect.bisect_left(ts_list, target_dt)
    candidates = [i for i in (idx - 1, idx) if 0 <= i < len(ts_list)]
    if not candidates:
        return None
    best = min(candidates, key=lambda i: abs(ts_list[i] - target_dt))
    if abs(ts_list[best] - target_dt) > dt.timedelta(minutes=tolerance_minutes):
        return None
    return cycles[best]


def _oi_wall_reading(oi_cycle, direction: str, strike_step: float = 50):
    """institutional_wall_score (OI-wall strength) + moneyness_score (used
    as the delta-confirmation proxy -- see module docstring's honest-data
    posture) for the wall that DEFENDS an open position of this direction:
    PE wall below price for a BUY, CE wall above price for a SELL. Returns
    (None, None) when there's no OI cycle joined for this candle."""
    if oi_cycle is None:
        return None, None
    cycle, rows = oi_cycle["cycle"], oi_cycle["rows"]
    atm = cycle.get("atm")
    if atm is None or not rows:
        return None, None
    wall_direction = "PE" if direction == "BUY" else "CE"
    result = analyze_oi_cluster(rows, atm, wall_direction, strike_step)
    if not result or not result.get("tradeable_data"):
        return None, None
    return result.get("institutional_wall_score"), result.get("moneyness_score")


# ---------------------------------------------------------------------------
# 1. Dynamic ATR + structure trailing stop
# ---------------------------------------------------------------------------

ATR_TRAIL_MULT = 1.5   # starting heuristic -- not yet empirically fit beyond this backtest


def update_trailing_stop(direction: str, current_sl: float, current_price: float,
                          atr: float, resistances: list, supports: list,
                          atr_trail_mult: float = None) -> float:
    """ATR-based trail blended with the nearest already-cleared structure
    rung, whichever is MORE protective (closer to current price) -- but
    never loosens the stop, regardless of which candidate wins this candle.

    atr_trail_mult: overrides ATR_TRAIL_MULT when given (backtest-only
    tuning, see app.py's ENGINE_PARAM_SPECS["dynamic-sr-v4"]) -- None (the
    default) preserves today's exact behavior; there is no live caller of
    this function today, so this is zero-risk to production either way."""
    atr_trail_mult = ATR_TRAIL_MULT if atr_trail_mult is None else atr_trail_mult
    candidates = []
    if atr:
        candidates.append(current_price - atr_trail_mult * atr if direction == "BUY"
                           else current_price + atr_trail_mult * atr)
    ladder = supports if direction == "BUY" else resistances
    cleared = [lvl for lvl in ladder if (lvl < current_price if direction == "BUY" else lvl > current_price)]
    if cleared:
        candidates.append(max(cleared) if direction == "BUY" else min(cleared))
    if not candidates:
        return current_sl
    new_floor = max(candidates) if direction == "BUY" else min(candidates)
    return max(current_sl, new_floor) if direction == "BUY" else min(current_sl, new_floor)


def check_breakeven_trigger(direction: str, entry: float, initial_sl: float, favorable_price: float,
                             atr: float, breakeven_trigger_r: float = None,
                             breakeven_trigger_atr_mult: float = None,
                             breakeven_tick_buffer: float = 0.0) -> float:
    """Break Even Engine (backtest-only knob, same status as max_sl_atr_mult
    above -- no live caller of manage_exit today). Returns the breakeven SL
    price once this candle's favorable excursion (intrabar high for BUY,
    low for SELL -- same "did price touch it" convention the target-hit
    ratchet above uses, not the close) has cleared the configured profit
    trigger; None if the trigger hasn't been reached yet, or if the feature
    is off (both trigger params None -- manage_exit already guards this
    before calling, this module-level check is redundant belt-and-braces).

    Exactly one trigger measure is meaningful at a time -- R-multiple
    (breakeven_trigger_r, e.g. 1.0/1.5/2.0 x this trade's OWN initial risk
    |entry - initial_sl|) or ATR-multiple (breakeven_trigger_atr_mult x
    entry_atr). If both are given, R takes precedence (a trade's own risk
    is the more directly meaningful "have I made back what I'm risking"
    measure; ATR is the fallback for when a caller wants a market-
    volatility-relative trigger instead).

    breakeven_tick_buffer: added past bare entry in the favorable direction
    (0.0 default = exact entry, a true scratch with zero edge either way,
    not a small guaranteed profit) -- the spec's "optional +1 tick
    protection". The caller (manage_exit) is responsible for ratcheting
    the returned price against current_sl (max for BUY / min for SELL) --
    this function only computes the candidate, it never loosens anything
    itself, same division of responsibility as update_trailing_stop above."""
    if breakeven_trigger_r is None and breakeven_trigger_atr_mult is None:
        return None
    moved = (favorable_price - entry) if direction == "BUY" else (entry - favorable_price)
    if breakeven_trigger_r is not None:
        risk = abs(entry - initial_sl)
        if risk <= 0:
            return None
        trigger_dist = breakeven_trigger_r * risk
    else:
        if not atr:
            return None
        trigger_dist = breakeven_trigger_atr_mult * atr
    if moved < trigger_dist:
        return None
    return entry + breakeven_tick_buffer if direction == "BUY" else entry - breakeven_tick_buffer


# ---------------------------------------------------------------------------
# 2. Structure-break exit
# ---------------------------------------------------------------------------

def check_structure_break(direction: str, originating_level: float, close_price: float) -> bool:
    """True if price has closed back through the level that originally
    triggered entry (a broken resistance failing to hold as new support, or
    vice versa) -- the setup's own premise has invalidated."""
    if originating_level is None:
        return False
    return close_price < originating_level if direction == "BUY" else close_price > originating_level


# ---------------------------------------------------------------------------
# 3. Opposite-signal exit
# ---------------------------------------------------------------------------

def check_opposite_signal(direction: str, candles: list, pdh: float, pdl: float, current_price: float) -> bool:
    """Re-runs dynamic_sr_engine.evaluate() UNCHANGED -- if it independently
    produces a valid, confidence-gated signal in the OPPOSITE direction,
    that's a genuine reversal (same quality bar as any fresh entry), not a
    reason invented just for exiting early."""
    result = evaluate_dynamic_sr(candles, pdh, pdl, current_price=current_price)
    sig = result.get("signal") if result else None
    if not sig:
        return False
    opposite = "SELL" if direction == "BUY" else "BUY"
    return sig["direction"] == opposite


# ---------------------------------------------------------------------------
# 4. VWAP-cross exit
# ---------------------------------------------------------------------------

VWAP_CROSS_ATR_BUFFER = 0.15   # "decisively" -- a token wick-tag on VWAP isn't a real cross

def check_vwap_cross(direction: str, close_price: float, vwap: float, atr: float) -> bool:
    """No-op (returns False) when vwap is None -- true for every pure index
    in this codebase (no tradable-volume feed, see calc_vwap), not a bug."""
    if vwap is None:
        return False
    buffer = VWAP_CROSS_ATR_BUFFER * atr if atr else 0.0
    return close_price < vwap - buffer if direction == "BUY" else close_price > vwap + buffer


# ---------------------------------------------------------------------------
# 5. Momentum-fade exit
# ---------------------------------------------------------------------------

MOMENTUM_FADE_THRESHOLD = 35.0   # starting heuristic

def compute_momentum_score(direction: str, candles: list, entry_oi_wall: float, current_oi_wall: float) -> float:
    """0-100. Blends whatever momentum evidence is actually available this
    candle -- ROC always is; volume-drop only for symbols with a real
    volume feed; OI-wall fade only when both an entry-time and current OI
    cycle were joined. Unavailable components are dropped from the average
    (not defaulted to neutral) -- with a real-time ROC always present,
    "no evidence available" never happens, unlike the Trade Quality Score's
    per-factor neutral-50 convention, which needs every factor to render a
    single comparable number across trades."""
    scores = []

    roc = momentum_roc(candles)
    roc_signed = roc if direction == "BUY" else -roc
    scores.append(_clamp01(roc_signed / 0.003 + 0.5) * 100)   # 0.3% 5-candle ROC ~= full marks, starting heuristic

    avg_vol = avg_volume(candles)
    latest_vol = candles[-1].get("volume", 0) or 0
    if avg_vol:   # has_volume_feed -- see dynamic_sr_engine.evaluate()'s identical gate
        scores.append(_clamp01(latest_vol / avg_vol) * 100)

    if entry_oi_wall is not None and current_oi_wall is not None:
        fade = entry_oi_wall - current_oi_wall   # positive = wall has weakened since entry
        scores.append(_clamp01(0.5 - fade / 100) * 100)

    return round(sum(scores) / len(scores), 1) if scores else 50.0


def check_momentum_fade(momentum_score: float, momentum_fade_threshold: float = None) -> bool:
    """momentum_fade_threshold: overrides MOMENTUM_FADE_THRESHOLD when given
    (backtest-only tuning) -- None preserves today's exact behavior."""
    threshold = MOMENTUM_FADE_THRESHOLD if momentum_fade_threshold is None else momentum_fade_threshold
    return momentum_score < threshold


# ---------------------------------------------------------------------------
# 6. Adaptive ATR/structure profit targets
# ---------------------------------------------------------------------------

ATR_TARGET_MULTS = (1.5, 3.0, 4.5)   # starting heuristic, sized to be reachable inside a 30-60min hold

def compute_adaptive_targets(direction: str, entry: float, atr: float, range1: float,
                              resistances: list, supports: list) -> list:
    """Each target is the CLOSER of an ATR-multiple projection and the
    nearest untouched structure rung -- whichever is more realistically
    reachable, rather than V1's fixed range1-ladder (hit in ~2% of trades
    per the V3 backtest, since range1 can be up to half the prior day's
    range -- far more than typical intraday movement)."""
    sign = 1 if direction == "BUY" else -1
    ladder = resistances if direction == "BUY" else supports
    beyond = sorted((lvl for lvl in ladder if (lvl > entry if direction == "BUY" else lvl < entry)),
                     key=lambda lvl: abs(lvl - entry))
    step = atr if atr else range1
    targets = []
    prev = entry
    for i, mult in enumerate(ATR_TARGET_MULTS):
        atr_candidate = entry + sign * mult * step
        candidates = [atr_candidate] + ([beyond[i]] if i < len(beyond) else [])
        chosen = min(candidates, key=lambda c: abs(c - entry))
        # keep monotonic even if a ladder rung happens to sit closer than the
        # previous stage's target (extend_levels' rungs aren't ATR-spaced)
        if (direction == "BUY" and chosen <= prev) or (direction == "SELL" and chosen >= prev):
            chosen = prev + sign * step * 0.5
        targets.append(round(chosen, 2))
        prev = chosen
    return targets


# ---------------------------------------------------------------------------
# 7. Adaptive max-hold (time exit is the LAST-RESORT fallback)
# ---------------------------------------------------------------------------

ADAPTIVE_HOLD_BASE_MINUTES = 30
ADAPTIVE_HOLD_MAX_MINUTES = 60

def compute_adaptive_max_hold(atr: float, current_price: float, adx: float,
                               adaptive_hold_base_minutes: float = None,
                               adaptive_hold_max_minutes: float = None) -> float:
    """30min baseline, extended toward 60min when the tape is unusually
    calm (low ATR% -- a slow trend has room to develop) AND/OR genuinely
    trending (high ADX). Starting heuristic -- the ATR% reference band
    (0.02%-0.10%) is calibrated off this engine's own July 2026 NIFTY 3m
    candle stats (median range ~12.2, ~0.05% of price), not yet validated
    on other symbols/regimes.

    adaptive_hold_base_minutes/adaptive_hold_max_minutes: override
    ADAPTIVE_HOLD_BASE_MINUTES/ADAPTIVE_HOLD_MAX_MINUTES when given
    (backtest-only tuning) -- None preserves today's exact behavior."""
    base = ADAPTIVE_HOLD_BASE_MINUTES if adaptive_hold_base_minutes is None else adaptive_hold_base_minutes
    cap = ADAPTIVE_HOLD_MAX_MINUTES if adaptive_hold_max_minutes is None else adaptive_hold_max_minutes
    if not current_price:
        return base
    atr_pct = (atr / current_price * 100) if atr else 0.05
    calm_factor = _clamp01((0.10 - atr_pct) / 0.08)
    trend_factor = _clamp01(((adx or 15) - 15) / 25)
    extension = 0.5 * calm_factor + 0.5 * trend_factor
    span = cap - base
    return round(base + extension * span)


# ---------------------------------------------------------------------------
# Trade Quality Score (0-100)
# ---------------------------------------------------------------------------

QUALITY_WEIGHTS = {
    "sr_strength": 1.2, "volume_quality": 0.8, "oi_wall_strength": 1.2,
    "trend_alignment": 1.2, "vwap_alignment": 0.8, "delta_confirmation": 1.0,
    "volatility_quality": 0.8, "market_structure_quality": 1.0,
}


def compute_trade_quality_score(direction: str, entry: float, level_price: float, atr: float,
                                 candles: list, pdh: float, pdl: float,
                                 vwap: float = None, oi_wall_score: float = None,
                                 delta_score: float = None) -> dict:
    """0-100 composite + the 8 named sub-scores (for display/debugging).
    Every sub-score defaults to a neutral 50 when its data isn't available
    (VWAP for a pure index, OI wall/delta without a joined cycle) so the
    composite is always well-defined and comparable across trades/symbols --
    see module docstring's "not applicable != violated" posture."""
    sub = {}

    breakout_dist = abs(entry - level_price) if level_price is not None else 0
    sub["sr_strength"] = round(_clamp01(breakout_dist / atr / 2) * 100, 1) if atr else 50.0

    avg_vol = avg_volume(candles)
    latest_vol = candles[-1].get("volume", 0) or 0
    sub["volume_quality"] = round(_clamp01(latest_vol / avg_vol / 1.5) * 100, 1) if avg_vol else 50.0

    sub["oi_wall_strength"] = oi_wall_score if oi_wall_score is not None else 50.0
    sub["delta_confirmation"] = delta_score if delta_score is not None else 50.0

    trend = detect_trend(candles)
    trend_agrees = (trend == "bullish") if direction == "BUY" else (trend == "bearish")
    _, _, adx = calc_adx(candles)
    sub["trend_alignment"] = round(_clamp01((adx or 15) / 40) * 100, 1) if trend_agrees else round(max(0.0, 50 - (adx or 15)), 1)

    if vwap is not None:
        close = candles[-1]["close"]
        favorable = (close - vwap) if direction == "BUY" else (vwap - close)
        sub["vwap_alignment"] = round(_clamp01(favorable / atr / 2 + 0.5) * 100, 1) if atr else 50.0
    else:
        sub["vwap_alignment"] = 50.0

    current_price = candles[-1]["close"]
    atr_pct = (atr / current_price * 100) if atr and current_price else None
    # peaks in a moderate band (~0.03-0.08%): too quiet = no follow-through
    # likely, too wild = unreliable/whipsaw-prone -- same reasoning as
    # compute_adaptive_max_hold's calm_factor.
    sub["volatility_quality"] = round(_clamp01(1 - abs((atr_pct or 0.05) - 0.05) / 0.10) * 100, 1) if atr_pct is not None else 50.0

    if pdh and pdl and pdh > pdl:
        rng = pdh - pdl
        pos_in_range = (current_price - pdl) / rng
        near_edge = pos_in_range if direction == "BUY" else (1 - pos_in_range)
        sub["market_structure_quality"] = round(_clamp01(near_edge) * 100, 1)
    else:
        sub["market_structure_quality"] = 50.0

    total_weight = sum(QUALITY_WEIGHTS.values())
    composite = sum(sub[k] * w for k, w in QUALITY_WEIGHTS.items()) / total_weight
    return {"composite": round(composite, 1), **sub}


def compute_predictive_confidence(quality_composite: float, breakout_dist: float, atr: float, adx: float) -> float:
    """Separate from dynamic_sr_engine.compute_confidence (the ENTRY gate,
    left untouched) -- this scores trade QUALITY for exit-side decisions
    and analysis, and is deliberately rescaled to spread across ~40-95
    rather than cluster like the entry score does (that score's breakout
    component saturates past range1/2.5, per its own formula, so most
    trades land in a narrow band regardless of how the trade actually
    performs -- see the V3 backtest's confidence-by-exit-reason breakdown,
    66.5/67.5/70.8, barely distinguishable)."""
    breakout_score = _clamp01(breakout_dist / atr / 3) * 100 if atr else 50.0
    trend_score = _clamp01(((adx or 15) - 10) / 30) * 100
    raw = 0.5 * quality_composite + 0.3 * breakout_score + 0.2 * trend_score
    return round(40 + _clamp01(raw / 100) * 55, 1)


# ---------------------------------------------------------------------------
# Position lifecycle: open + per-candle exit management
# ---------------------------------------------------------------------------

def open_position(signal: dict, entry_time, candles: list, pdh: float, pdl: float,
                   vwap: float = None, oi_cycle: dict = None, strike_step: float = 50,
                   adaptive_hold_base_minutes: float = None, adaptive_hold_max_minutes: float = None,
                   max_sl_atr_mult: float = None,
                   sizing_mode: str = None, capital: float = None, risk_pct: float = None,
                   fixed_qty: int = None, min_qty: int = None, max_qty: int = None) -> dict:
    """Builds a V4 position from a dynamic_sr_engine signal dict (unchanged
    entry). `candles` is the window ending at the entry candle.

    adaptive_hold_base_minutes/adaptive_hold_max_minutes: backtest-only
    tuning overrides, passed straight through to compute_adaptive_max_hold
    -- None preserves today's exact behavior.

    max_sl_atr_mult: backtest-only tuning override -- when given, caps the
    initial SL distance at max_sl_atr_mult * entry_atr (moves SL CLOSER to
    entry, never farther) for the rare case where the nearest ladder rung
    falls back to a distant PDH/PDL (dynamic_sr_engine.evaluate's SL is
    plain structural distance, unbounded by volatility). None preserves
    today's exact behavior -- the raw structural SL, uncapped.

    sizing_mode/capital/risk_pct/fixed_qty/min_qty/max_qty: forwarded
    as-is to position_sizing.compute_quantity (see that module's
    docstring for the full semantics) -- computed AFTER the max_sl_atr_mult
    cap above, so risk_pct sizing's per-unit-risk denominator is always
    this trade's REAL (possibly capped) stop distance. sizing_mode=None
    (default) preserves today's exact behavior: quantity is always 1."""
    direction = signal["direction"]
    entry = signal["entry"]
    atr = calc_atr(candles)
    _, _, adx = calc_adx(candles)
    levels = extend_levels(build_levels(pdh, pdl), entry)

    sl = signal["stop_loss"]
    if max_sl_atr_mult and atr:
        cap_dist = max_sl_atr_mult * atr
        sl = max(sl, entry - cap_dist) if direction == "BUY" else min(sl, entry + cap_dist)

    quantity = compute_quantity(entry, sl, sizing_mode=sizing_mode, capital=capital, risk_pct=risk_pct,
                                 fixed_qty=fixed_qty, min_qty=min_qty, max_qty=max_qty)

    oi_wall_score, delta_score = _oi_wall_reading(oi_cycle, direction, strike_step)
    quality = compute_trade_quality_score(
        direction, entry, signal["broken_level_price"], atr, candles, pdh, pdl,
        vwap=vwap, oi_wall_score=oi_wall_score, delta_score=delta_score,
    )
    breakout_dist = abs(entry - signal["broken_level_price"])
    predictive_confidence = compute_predictive_confidence(quality["composite"], breakout_dist, atr, adx)

    return {
        "direction": direction, "entry": entry, "entry_time": entry_time,
        "current_sl": sl, "initial_sl": sl,
        "originating_level": signal["broken_level_price"],
        "targets": compute_adaptive_targets(direction, entry, atr, signal["range1"], levels["resistances"], levels["supports"]),
        "best_target_hit": None,
        "breakeven_triggered": False,
        "entry_atr": atr, "entry_adx": adx,
        "entry_oi_wall_score": oi_wall_score, "entry_delta_score": delta_score,
        "max_hold_minutes": compute_adaptive_max_hold(atr, entry, adx, adaptive_hold_base_minutes, adaptive_hold_max_minutes),
        "confidence": signal["confidence"],
        "quality_score": quality["composite"], "quality_breakdown": quality,
        "predictive_confidence": predictive_confidence,
        "quantity": quantity,
    }


def manage_exit(position: dict, candle: dict, candles: list, pdh: float, pdl: float,
                 today: dt.date, oi_cycle: dict = None, strike_step: float = 50,
                 atr_trail_mult: float = None, momentum_fade_threshold: float = None,
                 breakeven_trigger_r: float = None, breakeven_trigger_atr_mult: float = None,
                 breakeven_tick_buffer: float = 0.0) -> dict:
    """Called once per candle for an open position. Returns
    {"exit": bool, "exit_reason": str|None, "exit_price": float|None,
    "position": dict} -- `position` carries forward the (possibly trailed)
    SL for the next call regardless of whether this candle exits.

    Checked in order: SL hit -> target hit -> break-even trigger -> structure
    break -> opposite signal -> VWAP cross -> momentum fade -> adaptive time
    exit (last resort, per module docstring priority 7) -> trailing-stop
    update LAST, from this candle's close, so it only ever affects the NEXT
    candle. SL/target/break-even checks use the stop as trailed by the
    PREVIOUS candle -- trailing first and checking the same candle's low/
    high against a stop computed from that same candle's own close would be
    lookahead (you can't know a candle's close before its low was reached
    intrabar).

    breakeven_trigger_r/breakeven_trigger_atr_mult/breakeven_tick_buffer:
    Break Even Engine (see check_breakeven_trigger above for full
    semantics) -- both trigger params None (the default) preserves today's
    exact behavior: the block below never fires, matching every other
    knob's "None = off" convention in this module."""
    direction = position["direction"]
    close_price = candle["close"]
    atr = calc_atr(candles) or position["entry_atr"]

    stopped = (candle["low"] <= position["current_sl"]) if direction == "BUY" else (candle["high"] >= position["current_sl"])
    if stopped:
        # current_sl only ever sits at/above entry (BUY) or at/below entry
        # (SELL) once a target has been reached OR the Break Even Engine has
        # fired -- see the ratchets below -- so a stop-out after that point
        # is never a real loss: it's either a flat scratch (best_target_hit
        # == 0, or breakeven_triggered with no target reached, SL ratcheted
        # to bare entry/+tick) or a locked-in partial profit (SL ratcheted
        # to a later target). Label it accordingly so it doesn't get
        # miscounted as a genuine loss alongside trades that never reached
        # any target and never hit their break-even trigger.
        best = position.get("best_target_hit")
        if best is not None:
            reason = "BREAKEVEN STOP" if best == 0 else "PROFIT-LOCKED STOP"
        elif position.get("breakeven_triggered"):
            reason = "BREAKEVEN STOP"
        else:
            reason = "STOP LOSS"
        return {"exit": True, "exit_reason": reason, "exit_price": position["current_sl"], "position": position}

    targets = position["targets"]
    hit_idx = None
    for ti, t in enumerate(targets):
        reached = (candle["high"] >= t) if direction == "BUY" else (candle["low"] <= t)
        if reached:
            hit_idx = ti
    prev_best = position.get("best_target_hit")
    # Only advance the ratchet -- a later candle that pulls back and only
    # re-clears a NEARER target than one already reached must never downgrade
    # best_target_hit/current_sl (see update_trailing_stop's "never loosens
    # the stop" invariant above; this loop feeds the same ratchet).
    if hit_idx is not None and (prev_best is None or hit_idx > prev_best):
        position["best_target_hit"] = hit_idx
        position["current_sl"] = ([position["entry"]] + targets)[hit_idx]
        if hit_idx == len(targets) - 1:
            return {"exit": True, "exit_reason": "TARGET HIT", "exit_price": targets[-1], "position": position}

    if breakeven_trigger_r is not None or breakeven_trigger_atr_mult is not None:
        favorable_price = candle["high"] if direction == "BUY" else candle["low"]
        be_sl = check_breakeven_trigger(direction, position["entry"], position["initial_sl"], favorable_price, atr,
                                         breakeven_trigger_r, breakeven_trigger_atr_mult, breakeven_tick_buffer)
        if be_sl is not None:
            # Same ratchet convention as everywhere else in this file --
            # take the better of what's already there vs. the new
            # candidate, never the reverse.
            position["current_sl"] = (max(position["current_sl"], be_sl) if direction == "BUY"
                                       else min(position["current_sl"], be_sl))
            position["breakeven_triggered"] = True

    if check_structure_break(direction, position["originating_level"], close_price):
        return {"exit": True, "exit_reason": "STRUCTURE BREAK", "exit_price": close_price, "position": position}

    if check_opposite_signal(direction, candles, pdh, pdl, close_price):
        return {"exit": True, "exit_reason": "OPPOSITE SIGNAL", "exit_price": close_price, "position": position}

    vwap = calc_vwap(candles, today)
    if check_vwap_cross(direction, close_price, vwap, atr):
        return {"exit": True, "exit_reason": "VWAP CROSS", "exit_price": close_price, "position": position}

    oi_wall_score, _ = _oi_wall_reading(oi_cycle, direction, strike_step)
    momentum_score = compute_momentum_score(direction, candles, position["entry_oi_wall_score"], oi_wall_score)
    if check_momentum_fade(momentum_score, momentum_fade_threshold):
        return {"exit": True, "exit_reason": "MOMENTUM FADE", "exit_price": close_price, "position": position}

    held_min = (candle["datetime"] - position["entry_time"]).total_seconds() / 60
    if held_min >= position["max_hold_minutes"]:
        return {"exit": True, "exit_reason": "TIME EXIT", "exit_price": close_price, "position": position}

    levels = extend_levels(build_levels(pdh, pdl), close_price)
    position["current_sl"] = update_trailing_stop(
        direction, position["current_sl"], close_price, atr, levels["resistances"], levels["supports"],
        atr_trail_mult)

    return {"exit": False, "exit_reason": None, "exit_price": None, "position": position}
