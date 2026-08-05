#!/usr/bin/env python3
"""
sr_engine_v3.py -- Support & Resistance Engine V3: an Adaptive Institutional
Probability Engine. Fuses Price Action (Swing/VWAP/CPR/Pivot/Trend) + Option
Chain OI clustering (OI, OI-change, Volume, Liquidity, Delta, Gamma, Theta,
IV) + a previous-day validation/extension lifecycle into WEIGHTED confidence
scores -- never a binary pass/fail gate (see "Weighted, not hard-filtered"
below).

Pure, side-effect-free (same contract as oi_engine.py/scalping_engine.py --
no network/DB calls; callers pass in whatever's already been fetched this
cycle, plus the previous trading day's closing rows for previous-day
validation). Runs in PARALLEL to V1 (sr_probability_engine.py), Engine V2,
and the Scalping Engine -- never modifies any of them, and reuses their
already-tested building blocks wherever the same evidence is needed
(dynamic target/SL math, volume-expansion, non-repaint candle-close
confirmation, R:R validation) instead of reimplementing it -- same
"one tested implementation, not several drifting copies" rule the rest of
this codebase follows (see oi_engine.py's own docstring).

WEIGHTED, NOT HARD-FILTERED (2026-08-02 upgrade, per explicit request):
- The "hold vs extend" call for each side is a CONTINUOUS comparison
  (extend-evidence score vs hold-probability score) -- never a boolean
  "extending==True" gate. Whichever is higher wins; there is no minimum
  either has to clear to be CONSIDERED (only the overall confidence
  threshold gates whether a side is evaluated as a candidate at all).
- Candle confirmation (confirms_reversal_entry) is applied ONLY at final
  execution -- it decides `execution_confirmed`/`tradeable`, but NEVER
  removes the underlying weighted trade_decision/confidence/entry/target/SL
  from the output. A "hold" setup that clears confidence but hasn't been
  candle-confirmed yet is reported ARMED (decision + full plan visible,
  tradeable=False) rather than silently discarded -- this is what preserves
  trade frequency/visibility while still gating real execution.
- Indicator weights (analyze_oi_cluster's factor_weights) are pluggable and
  auto-adjustable from genuine historical outcomes (see
  learn_adaptive_weights) -- bounded, transparent nudges, gated on a minimum
  sample size so a thin sample can't overfit the weights to noise.

HONEST DATA LIMITATION (same posture as
sr_probability_engine.score_strike_candidates's docstring): this data source
has no real bid/ask spread or order-book depth. "Liquidity" below is NSE's
ce_buy_qty/ce_sell_qty (total buy/sell quantity) order-flow SKEW when
available live -- NOT a genuine spread, and it is NOT persisted to the
historical DB, so backtest/previous-day-validation replay runs with
liquidity_score=None rather than a faked number. Every score below degrades
its data_completeness honestly rather than guessing. Similarly, Gamma/Theta/
Delta/Volume weighting below are STARTING HEURISTICS (see each function's
docstring) -- normalized RELATIVE TO THE CLUSTER'S OWN AVERAGE (scale-free
across instruments of very different spot prices) rather than an invented
absolute scale, but not yet empirically validated -- same honesty as
scalping_engine.py's SCALP_* constants and oi_engine.compute_new_trend_meter's
docstring.
"""

import math
import datetime as dt

from sr_probability_engine import (
    compute_volume_expansion, check_candle_close_confirmation,
    compute_dynamic_targets_sl, validate_risk_reward,
)

V3_MIN_RISK_REWARD = 1.4
V3_HOLD_PROB_FLOOR, V3_HOLD_PROB_CEIL = 5, 95       # never claim absolute 0%/100% certainty
V3_CONFIDENCE_TRADE_THRESHOLD = 60

# Target/SL sizing calibrated for V3's ACTUAL hold window (30min, extendable
# to 60min via should_pause_time_exit) -- added 2026-08-02 after backtesting
# showed the swing engine's default min_target_pct (15%, sized for an
# unlimited/EOD hold) produced R:R of 3-10x, meaning the target was almost
# NEVER reached inside 30-60 minutes regardless of how right the direction
# call was (avg MFE stayed well under target on nearly every trade) --
# a real sizing bug, not a signal-quality problem. Sits between the Scalp
# Engine's SCALP_MIN_TARGET_PCT=0.06 (6-min hold) and the swing engine's 0.15
# (unlimited hold) -- proportionally reasonable for a hold roughly 5-10x
# longer than the scalp engine's. Starting values, not yet empirically
# fit beyond this backtest -- same honesty as every other constant here.
V3_MIN_TARGET_PCT = 0.08
V3_MAX_SL_PCT = 0.04
V3_EXTENSION_MAX_EVIDENCE = 6                        # detect_*_extend_*'s max possible evidence items -- used to normalize its 0-100 "extend score"
V3_VALID_WALL_MIN, V3_VALID_FAKE_MAX = 60, 35        # previous-day-validation VALID bucket
V3_WEAK_WALL_MIN, V3_WEAK_FAKE_MAX = 35, 60           # previous-day-validation WEAK bucket (below this -> FAILED)

# Pluggable, auto-adjustable indicator weights (see learn_adaptive_weights) --
# neutral (1.0) starting multipliers on analyze_oi_cluster's component scores.
# Bounded to [0.5, 1.5] by learn_adaptive_weights so no single factor can ever
# collapse to zero or dominate the score, however the sample evolves.
V3_DEFAULT_FACTOR_WEIGHTS = {
    "strength": 1.0, "writing": 1.0, "liquidity": 1.0, "volume": 1.0,
    "moneyness": 1.0, "theta_defense": 1.0, "iv_penalty": 1.0, "gamma_instability": 1.0,
}
V3_WEIGHT_BOUNDS = (0.5, 1.5)
V3_WEIGHT_LEARN_STEP = 0.05
V3_WEIGHT_MIN_SAMPLE = 20   # minimum CLOSED (TARGET HIT/STOP LOSS) trades before nudging ANY weight away from neutral


def _clamp01(x):
    return max(0.0, min(1.0, x))


def _weighted_avg(items):
    """
    Importance-weighted average over (score, weight) pairs, renormalized
    over only the AVAILABLE (score is not None) items -- e.g. if a 2.0-weight
    item is missing this cycle, the remaining items' weights still sum to
    whatever they are, so the average reflects ONLY genuinely available
    evidence (same "don't fake missing data" honesty as everywhere else in
    this module) without being diluted by simply averaging in more terms.
    Returns 0.0 if nothing is available.
    """
    available = [(s, w) for s, w in items if s is not None and w > 0]
    if not available:
        return 0.0
    total_w = sum(w for _, w in available)
    return sum(s * w for s, w in available) / total_w


_ADJUST_CAP = 10.0   # max points any single bounded-adjustment factor can shift a score by


def _bounded_adjustment(score, weight, max_points=_ADJUST_CAP):
    """
    Converts a 0-100 factor reading into a small, bounded +/- adjustment
    (centered on 50 = neutral) rather than pooling it into an average --
    see analyze_oi_cluster's institutional_wall_score/fake_index for why:
    genuinely weighted-in, but capped so it can never swamp the core signal
    it's adjusting. Returns 0.0 if the factor isn't available this cycle
    (honest no-op, not a fabricated neutral score).
    """
    if score is None:
        return 0.0
    return (score - 50) / 50 * max_points * weight


# ---------------------------------------------------------------------------
# 1. Raw level detection -> Dynamic Support / Resistance
# ---------------------------------------------------------------------------

def _weighted_level(candidates):
    """
    Weighted-average price across every genuinely-available candidate --
    NOT a pick-one/reject-rest filter (per spec: weighted scoring, not hard
    filters). completeness = fraction of the max possible category-weight
    actually available this cycle, used later to scale confidence down
    honestly when fewer categories fired (same spirit as
    sr_probability_engine's data_completeness).
    """
    if not candidates:
        return None, 0.0
    total_weight = sum(w for _, _, w in candidates)
    price = sum(p * w for _, p, w in candidates) / total_weight
    max_possible_weight = 1.0 + 0.9 + 0.7 + 0.6 + 0.5   # PDH/PDL + Swing + Pivot + CPR + VWAP
    completeness = min(1.0, total_weight / max_possible_weight)
    return round(price, 2), round(completeness, 2)


def compute_v3_raw_levels(market_structure, underlying):
    """
    Weighted fusion of every genuinely-available structural level (Swing,
    VWAP, CPR, Pivot, prior-day-range/"Trend" levels) into one Dynamic
    Support / Dynamic Resistance price. Falls back to level=None (never a
    fabricated price) when nothing is available yet (e.g. first day of
    candle collection, same degrade-gracefully pattern as
    market_structure.build_market_structure).
    """
    ms = market_structure or {}
    pivots = ms.get("pivots") or {}
    cpr = ms.get("cpr") or {}
    vwap = ms.get("vwap")
    swing_h, swing_l = ms.get("swing_high"), ms.get("swing_low")
    custom = ms.get("custom_levels") or {}
    regime = ms.get("regime")

    # (name, price, weight) -- weight reflects how much this category is
    # trusted as genuine institutional structure vs a softer/derived read.
    resistance_candidates, support_candidates = [], []

    if custom.get("resistance"):
        resistance_candidates.append(("PDH", custom["resistance"], 1.0))
    if custom.get("support"):
        support_candidates.append(("PDL", custom["support"], 1.0))
    if swing_h:
        resistance_candidates.append(("Swing High", swing_h, 0.9))
    if swing_l:
        support_candidates.append(("Swing Low", swing_l, 0.9))
    if pivots.get("R1"):
        resistance_candidates.append(("Pivot R1", pivots["R1"], 0.7))
    if pivots.get("S1"):
        support_candidates.append(("Pivot S1", pivots["S1"], 0.7))
    if cpr.get("tc"):
        resistance_candidates.append(("CPR TC", cpr["tc"], 0.6))
    if cpr.get("bc"):
        support_candidates.append(("CPR BC", cpr["bc"], 0.6))
    if vwap and underlying:
        # VWAP only counts as a resistance candidate while price is
        # currently BELOW it (acting as overhead resistance right now), and
        # as a support candidate while price is above it -- mirrors how VWAP
        # actually functions as an intraday S/R level (tested from one side),
        # not a fixed level regardless of where price currently sits.
        if underlying < vwap:
            resistance_candidates.append(("VWAP", vwap, 0.5))
        else:
            support_candidates.append(("VWAP", vwap, 0.5))

    dynamic_resistance, resistance_completeness = _weighted_level(resistance_candidates)
    dynamic_support, support_completeness = _weighted_level(support_candidates)

    return {
        "dynamic_resistance": dynamic_resistance, "resistance_inputs": resistance_candidates,
        "resistance_completeness": resistance_completeness,
        "dynamic_support": dynamic_support, "support_inputs": support_candidates,
        "support_completeness": support_completeness,
        "regime": regime,
    }


def map_levels_to_strikes(dynamic_support, dynamic_resistance, strike_step):
    """Nearest PE strike AT/BELOW dynamic_support -> support_strike (floor to
    strike_step, so the mapped strike is genuinely at-or-below the level, not
    straddling it); nearest CE strike AT/ABOVE dynamic_resistance ->
    resistance_strike (ceil to strike_step)."""
    support_strike = int(dynamic_support // strike_step) * strike_step if dynamic_support is not None else None
    resistance_strike = (
        int(math.ceil(dynamic_resistance / strike_step)) * strike_step if dynamic_resistance is not None else None
    )
    return support_strike, resistance_strike


# ---------------------------------------------------------------------------
# 2. Cluster analysis (center strike +/- 3, "OI Cluster")
# ---------------------------------------------------------------------------

def analyze_oi_cluster(rows, center_strike, direction, strike_step, factor_weights=None):
    """
    ATM+/-3-strike cluster analysis around `center_strike` for `direction`
    ("CE" for a resistance wall, "PE" for a support wall). Returns weighted
    0-100 scores -- see module docstring: weighted scoring, never a hard
    pass/fail gate. When called for the support strike with direction="PE",
    the returned `fake_index` IS the spec's "Fake Support Index"; called for
    the resistance strike with direction="CE", it's the "Fake Resistance
    Index" -- same function, scoped by which side it's evaluating.
    Degrades honestly (component excluded, data_completeness lowered) when a
    category isn't available (e.g. Greeks missing this cycle, or
    buy/sell-qty absent in a backtest replay -- see module docstring).

    factor_weights: optional {factor_name: multiplier} dict (see
    V3_DEFAULT_FACTOR_WEIGHTS / learn_adaptive_weights) -- defaults to
    neutral 1.0 for every factor when omitted, so existing callers are
    unaffected until they opt into learned weights.

    Inputs used (per spec: OI, OI Change, Volume, Liquidity, Delta, Gamma,
    Theta, IV -- all weighted, all normalized RELATIVE TO THE CLUSTER'S OWN
    AVERAGE so the same code works across instruments of very different spot
    prices without an invented absolute scale):
      - OI / OI-change -> support_resistance_strength, oi_cluster_strength, writing_score
      - Volume -> volume_score (genuine active interest vs stale resting OI)
      - Liquidity (buy/sell qty skew) -> liquidity_score
      - Delta -> moneyness_score (ATM strikes, where delta~0.5, are weighted
        as more institutionally meaningful than deep OTM/ITM ones)
      - Gamma -> gamma_instability (feeds the Fake Index: elevated gamma
        relative to the cluster means dealer-hedging can accelerate a move
        THROUGH the level, not just defend it)
      - Theta -> theta_defense_score (elevated time-decay relative to the
        cluster means writers have a growing incentive to defend as time
        passes -- feeds the Institutional Wall Score)
      - IV -> iv_penalty (very high IV = crowded/unstable positioning)
    """
    if center_strike is None:
        return None
    weights = {**V3_DEFAULT_FACTOR_WEIGHTS, **(factor_weights or {})}
    row_by_strike = {r.strike: r for r in rows}
    offsets = [-3, -2, -1, 0, 1, 2, 3]
    cluster_rows = [(off, row_by_strike.get(center_strike + off * strike_step)) for off in offsets]
    present = [(off, r) for off, r in cluster_rows if r is not None]
    if not present:
        return {"tradeable_data": False, "center_strike": center_strike, "direction": direction,
                "reason": "No strikes in this cluster this cycle."}

    def oi(r): return (r.ce_oi if direction == "CE" else r.pe_oi) or 0
    def oi_chg(r): return (r.ce_oi_chg if direction == "CE" else r.pe_oi_chg) or 0
    def vol(r): return (r.ce_vol if direction == "CE" else r.pe_vol) or 0
    def delta(r): return (r.ce_delta if direction == "CE" else r.pe_delta) or 0.0
    def gamma(r): return (r.ce_gamma if direction == "CE" else r.pe_gamma) or 0.0
    def theta(r): return (r.ce_theta if direction == "CE" else r.pe_theta) or 0.0
    def iv(r): return (r.ce_iv if direction == "CE" else r.pe_iv) or None
    def buy_qty(r): return (r.ce_buy_qty if direction == "CE" else r.pe_buy_qty) or 0
    def sell_qty(r): return (r.ce_sell_qty if direction == "CE" else r.pe_sell_qty) or 0

    center_row = row_by_strike.get(center_strike)
    total_cluster_oi = sum(oi(r) for _, r in present) or 1
    center_oi = oi(center_row) if center_row else 0
    avg_oi = total_cluster_oi / len(present)

    # Support/Resistance Strength: how concentrated OI is AT the level's own
    # strike relative to the cluster average -- a genuine wall sits well
    # above the cluster average, not just "has some OI".
    strength = round(_clamp01(center_oi / avg_oi / 3) * 100, 1) if avg_oi else 0.0

    # OI Cluster Strength: how much of the cluster's total OI sits within +/-1
    # of center (tight = institutional positioning at ONE spot; spread across
    # all 7 strikes = diffuse/weak wall).
    near = sum(oi(r) for off, r in present if abs(off) <= 1)
    cluster_strength = round(near / total_cluster_oi * 100, 1) if total_cluster_oi else 0.0

    # Volume: genuine ACTIVE interest at this strike vs the cluster average --
    # high resting OI with no volume behind it is a much weaker signal than
    # the same OI with real trading activity. Same center/average/3 pattern
    # as OI strength (scale-free, no invented absolute volume threshold).
    total_cluster_vol = sum(vol(r) for _, r in present)
    center_vol = vol(center_row) if center_row else 0
    avg_vol = total_cluster_vol / len(present) if present else 0
    volume_score = round(_clamp01(center_vol / avg_vol / 3) * 100, 1) if avg_vol else None

    # Delta -> moneyness_score: strikes near delta=0.5 (genuinely ATM) are
    # weighted as more institutionally meaningful than deep OTM/ITM ones,
    # where OI is more often stale/hedged-elsewhere rather than a genuine
    # directional wall. Peaks at 100 when |delta|=0.5, falls off linearly.
    delta_val = delta(center_row) if center_row else None
    moneyness_score = None
    if delta_val:
        moneyness_score = round(max(0.0, 100 * (1 - abs(abs(delta_val) - 0.5) * 2)), 1)

    # Gamma -> gamma_instability: elevated gamma AT this strike relative to
    # the cluster average means dealer/writer hedging is more sensitive here
    # -- if price actually reaches the level, hedging flows can ACCELERATE a
    # break rather than absorb it. Feeds the Fake Index, not the Wall Score.
    total_gamma = sum(gamma(r) for _, r in present)
    center_gamma = gamma(center_row) if center_row else 0
    avg_gamma = total_gamma / len(present) if present else 0
    gamma_instability = round(_clamp01(center_gamma / avg_gamma / 3) * 100, 1) if avg_gamma else None

    # Theta -> theta_defense_score: elevated time-decay AT this strike
    # relative to the cluster means the writers defending it gain more from
    # time simply passing without a move -- a growing incentive to defend.
    # Feeds the Wall Score. Uses magnitude (theta is conventionally negative).
    total_theta = sum(abs(theta(r)) for _, r in present)
    center_theta = abs(theta(center_row)) if center_row else 0
    avg_theta = total_theta / len(present) if present else 0
    theta_defense_score = round(_clamp01(center_theta / avg_theta / 3) * 100, 1) if avg_theta else None

    # Institutional Wall Score: OI magnitude (strength) + FRESH buildup
    # direction (writing, not stale resting OI) + liquidity-proxy skew +
    # volume + theta-defense -- each individually weighted (factor_weights),
    # then averaged -- minus a mild high-IV penalty.
    fresh_oi_chg = oi_chg(center_row) if center_row else 0
    writing_score = round(_clamp01(0.5 + (fresh_oi_chg / max(center_oi, 1)) * 2) * 100, 1) if center_row else 50.0
    bq, sq = (buy_qty(center_row), sell_qty(center_row)) if center_row else (0, 0)
    liquidity_score = None
    if bq or sq:
        total_qty = bq + sq
        # Order-flow SKEW toward the option-SELLER side (sell_qty dominance)
        # reads as "genuinely defended" -- this is NOT a real bid/ask spread,
        # see module docstring.
        liquidity_score = round(sq / total_qty * 100, 1) if total_qty else None
    iv_val = iv(center_row) if center_row else None
    iv_penalty = 0.0
    if iv_val is not None and iv_val > 0:
        iv_penalty = min(20.0, max(0.0, (iv_val - 25) * 0.5)) * weights["iv_penalty"]   # starting heuristic, penalizes IV above 25%

    # Institutional Wall Score = CORE score (OI strength + fresh writing +
    # liquidity -- the ORIGINAL, dominant signal, unchanged in magnitude from
    # before this cycle's Volume/Delta/Gamma/Theta additions) PLUS a small
    # BOUNDED adjustment from the newer factors -- NOT pooled into the same
    # average. Pooling was tried first and found to systematically DILUTE the
    # score every time a new factor was added (more terms in an average pulls
    # it toward whatever those new terms read, even when they're just noise
    # on a synthetic/thin sample), silently collapsing real trade frequency
    # even though nothing about the core OI-wall evidence changed -- exactly
    # the "preserve trade frequency" regression this design avoids. Each new
    # factor can shift the score by at most +/-`_ADJUST_CAP` points (centered
    # on its own 50 = neutral reading), genuinely weighted-in but never able
    # to swamp the core evidence.
    core_wall_items = [
        (strength, 2.0 * weights["strength"]),
        (writing_score, 2.0 * weights["writing"]),
        (liquidity_score, 1.0 * weights["liquidity"]),
    ]
    wall_adjustment = (_bounded_adjustment(volume_score, weights["volume"])
                       + _bounded_adjustment(moneyness_score, weights["moneyness"])
                       + _bounded_adjustment(theta_defense_score, weights["theta_defense"]))
    institutional_wall_score = round(max(0.0, min(100.0, _weighted_avg(core_wall_items) + wall_adjustment - iv_penalty)), 1)

    # Fake Support/Resistance Index: inverse signal -- high resting OI but
    # ALREADY unwinding at the defending strike, thin liquidity, high IV.
    # Gamma contributes as the same kind of small bounded adjustment (elevated
    # gamma near the level raises the fake-index a little; it never dominates
    # the core unwind/liquidity/IV evidence), same reasoning as the wall score.
    unwind_signal = 100.0 if (center_row and fresh_oi_chg < 0) else 0.0
    core_fake_items = [
        (unwind_signal, 2.0),
        (100 - liquidity_score if liquidity_score is not None else None, 1.0),
        (min(100.0, iv_penalty * 5), 1.0),
    ]
    fake_adjustment = _bounded_adjustment(gamma_instability, weights["gamma_instability"])
    fake_index = round(max(0.0, min(100.0, _weighted_avg(core_fake_items) + fake_adjustment)), 1)

    available_categories = [
        center_row is not None, liquidity_score is not None, bool(iv_val),
        volume_score is not None, moneyness_score is not None,
        gamma_instability is not None, theta_defense_score is not None,
    ]
    data_completeness = round(sum(available_categories) / len(available_categories), 2)

    return {
        "tradeable_data": True, "center_strike": center_strike, "direction": direction,
        "support_resistance_strength": strength,
        "oi_cluster_strength": cluster_strength,
        "institutional_wall_score": institutional_wall_score,
        "fake_index": fake_index,
        "liquidity_score": liquidity_score,
        "liquidity_note": ("NSE buy/sell quantity skew (order-flow proxy) -- not a real bid/ask spread."
                            if liquidity_score is not None else "Unavailable this cycle/replay."),
        "volume_score": volume_score, "moneyness_score": moneyness_score,
        "gamma_instability": gamma_instability, "theta_defense_score": theta_defense_score,
        "iv_at_level": iv_val,
        "data_completeness": data_completeness,
        "cluster_total_oi": total_cluster_oi, "center_oi": center_oi, "fresh_oi_chg": fresh_oi_chg,
    }


# ---------------------------------------------------------------------------
# OI Cluster Shift / Institutional Wall Migration
# ---------------------------------------------------------------------------

def compute_oi_cluster_center(rows, center_strike, direction, strike_step):
    """
    OI-weighted center-of-mass strike across the ATM+/-3 cluster -- a
    continuous, sub-strike-resolution read of WHERE the cluster's weight
    actually sits (not just which single strike has the most OI). Used by
    detect_wall_migration to track whether institutional positioning is
    drifting toward/away from price over time (OI Cluster Shift /
    Institutional Wall Migration). Returns None if there's no OI data in
    the cluster this cycle.
    """
    if center_strike is None:
        return None
    row_by_strike = {r.strike: r for r in rows}
    total_oi, weighted_sum = 0, 0
    for off in (-3, -2, -1, 0, 1, 2, 3):
        strike = center_strike + off * strike_step
        row = row_by_strike.get(strike)
        if not row:
            continue
        oi = (row.ce_oi if direction == "CE" else row.pe_oi) or 0
        total_oi += oi
        weighted_sum += oi * strike
    if total_oi <= 0:
        return None
    return round(weighted_sum / total_oi, 2)


def detect_wall_migration(center_history, current_center, strike_step, min_shift_steps=0.5):
    """
    Detects Institutional Wall Migration / OI Cluster Shift: compares the
    OI-weighted cluster center NOW vs the EARLIEST reading in the caller-
    owned recent-history buffer (PRIOR readings only -- same "append after
    evaluating" contract as compute_premium_entry_trigger elsewhere in this
    codebase). A sustained drift of at least `min_shift_steps` strike-steps
    is genuine migration evidence; a single-cycle wobble is not.

    UP: the wall is migrating toward/past higher strikes (writers rolling
    up -- often precedes/confirms an upside move). DOWN: mirrored.
    Returns {"migrating": "UP"/"DOWN"/"STABLE"/None, "shift_strikes": float}.
    """
    if not center_history or current_center is None:
        return {"migrating": None, "shift_strikes": None}
    baseline = center_history[0]
    if baseline is None:
        return {"migrating": None, "shift_strikes": None}
    shift = (current_center - baseline) / strike_step
    if shift >= min_shift_steps:
        migrating = "UP"
    elif shift <= -min_shift_steps:
        migrating = "DOWN"
    else:
        migrating = "STABLE"
    return {"migrating": migrating, "shift_strikes": round(shift, 2)}


# ---------------------------------------------------------------------------
# Regime auto-adaptation (Trend / Range / Volatile)
# ---------------------------------------------------------------------------

def classify_volatility(market_structure, underlying):
    """
    Volatility dimension (Trend/Range/VOLATILE, per spec) -- ATR as a % of
    underlying price, a scale-free read across instruments. Starting
    heuristic thresholds (>=0.6% = HIGH, <=0.2% = LOW), not yet empirically
    validated -- same honesty as the rest of this module. Returns "UNKNOWN"
    gracefully when ATR/underlying aren't available yet.
    """
    ms = market_structure or {}
    atr = ms.get("atr_14")
    if not atr or not underlying:
        return "UNKNOWN"
    atr_pct = atr / underlying * 100
    if atr_pct >= 0.6:
        return "HIGH"
    if atr_pct <= 0.2:
        return "LOW"
    return "NORMAL"


def v3_regime_weights(market_structure, underlying=None):
    """
    Regime-adaptive weighting (mirrors scalping_engine.volatility_regime_multiplier
    and oi_engine.compute_trend_meter's momentum_mult): in a TRENDING tape,
    break/extension evidence is weighted up (breakouts more likely genuine);
    in a RANGING tape, hold/fake-index evidence is weighted up (breakouts
    more likely a fakeout). ADDITIONALLY cross-cut by volatility (Trend /
    Range / VOLATILE, per spec): a HIGH-volatility tape nudges break_mult up
    further (real follow-through more likely in an actual expansion), a LOW-
    volatility tape nudges hold_mult up (less likely to see a genuine
    breakout in a dead-quiet tape). Bounded multipliers so a missing/
    degenerate regime or volatility reading never produces an extreme output.
    """
    regime = (market_structure or {}).get("regime")
    volatility = classify_volatility(market_structure, underlying)

    if regime == "TRENDING":
        break_mult, hold_mult, label = 1.25, 0.85, "TRENDING"
    elif regime == "RANGING":
        break_mult, hold_mult, label = 0.8, 1.2, "RANGING"
    else:
        break_mult, hold_mult, label = 1.0, 1.0, "UNKNOWN/TRANSITIONING"

    if volatility == "HIGH":
        break_mult *= 1.1
        label += "-VOLATILE"
    elif volatility == "LOW":
        hold_mult *= 1.1
        label += "-CALM"

    break_mult = max(0.6, min(1.6, break_mult))
    hold_mult = max(0.6, min(1.6, hold_mult))
    return {"break_mult": break_mult, "hold_mult": hold_mult, "volatility": volatility,
            "label": f"{label} regime"}


def compute_hold_break_probability(cluster_eval, regime_weights):
    """Hold/Break probability from institutional wall score vs fake index,
    regime-adjusted. Bounded to [V3_HOLD_PROB_FLOOR, V3_HOLD_PROB_CEIL] --
    never claims absolute certainty either way."""
    if not cluster_eval or not cluster_eval.get("tradeable_data"):
        return None, None
    wall, fake = cluster_eval["institutional_wall_score"], cluster_eval["fake_index"]
    raw_hold = wall * regime_weights["hold_mult"] - fake * regime_weights["break_mult"] * 0.5
    hold_pct = max(V3_HOLD_PROB_FLOOR, min(V3_HOLD_PROB_CEIL, round(50 + raw_hold * 0.5, 1)))
    break_pct = round(100 - hold_pct, 1)
    return hold_pct, break_pct


def compute_v3_confidence(cluster_eval, level_completeness):
    """
    0-100 confidence -- weighted combination of the cluster evaluation's own
    wall/fake/cluster-strength scores, scaled DOWN by both the cluster's own
    data_completeness and the raw dynamic-level's completeness (a confident-
    looking cluster score computed from a poorly-supported dynamic level
    should not read as high confidence -- same "completeness x decisiveness"
    philosophy as sr_probability_engine._confidence_label).
    """
    if not cluster_eval or not cluster_eval.get("tradeable_data"):
        return 0
    base = (cluster_eval["institutional_wall_score"] * 0.5 + (100 - cluster_eval["fake_index"]) * 0.3
            + cluster_eval["oi_cluster_strength"] * 0.2)
    combined = base * (0.6 + 0.4 * cluster_eval["data_completeness"]) * (0.6 + 0.4 * (level_completeness or 0))
    return round(max(0, min(100, combined)))


# ---------------------------------------------------------------------------
# Level ladder (1st/2nd/3rd/4th steps: Pivot R1-R4 / S1-S4 + the DYNAMIC
# weighted-fusion level) -- added 2026-08-01 after backtesting NIFTY's real
# July 2026 option-chain data showed the single farthest DYNAMIC level
# (PDH/Swing/VWAP/CPR fusion) very often maps to a strike OUTSIDE the ATM+/-4
# window the live fetch actually captures (9 strikes/cycle) -- confirmed
# against real logged cycles, not a guess. Evaluating a graduated ladder of
# CLOSER candidate levels too (not replacing the DYNAMIC one, adding to it)
# gives the real OI-cluster scoring far more chances to find a genuinely
# in-window, well-scored level -- same weighted-selection philosophy, just
# with more candidates to choose the best of.
# ---------------------------------------------------------------------------

def build_level_ladder(market_structure, underlying, strike_step):
    """
    Ordered candidate ladder for BOTH sides: Pivot R1-R4 / S1-S4 (the spec's
    "1st/2nd/3rd/4th steps") PLUS the weighted DYNAMIC fusion level from
    compute_v3_raw_levels, each mapped to its nearest tradeable strike and
    sorted by distance from `underlying` (nearest first) -- nearest-first
    ordering maximizes the chance of landing inside the fetched strike
    window regardless of which specific pivot happens to be closest that day.
    """
    ms = market_structure or {}
    pivots = ms.get("pivots") or {}
    raw = compute_v3_raw_levels(ms, underlying)

    resistance_candidates, support_candidates = [], []
    for label in ("R1", "R2", "R3", "R4"):
        price = pivots.get(label)
        if price:
            resistance_candidates.append({"label": label, "price": price})
    if raw["dynamic_resistance"] is not None:
        resistance_candidates.append({"label": "DYNAMIC", "price": raw["dynamic_resistance"]})
    for label in ("S1", "S2", "S3", "S4"):
        price = pivots.get(label)
        if price:
            support_candidates.append({"label": label, "price": price})
    if raw["dynamic_support"] is not None:
        support_candidates.append({"label": "DYNAMIC", "price": raw["dynamic_support"]})

    for c in resistance_candidates:
        c["strike"] = int(math.ceil(c["price"] / strike_step)) * strike_step
    for c in support_candidates:
        c["strike"] = int(c["price"] // strike_step) * strike_step

    if underlying is not None:
        resistance_candidates.sort(key=lambda c: abs(c["strike"] - underlying))
        support_candidates.sort(key=lambda c: abs(c["strike"] - underlying))

    return {
        "resistance_candidates": resistance_candidates, "support_candidates": support_candidates,
        "dynamic_resistance": raw["dynamic_resistance"], "dynamic_support": raw["dynamic_support"],
        "resistance_completeness": raw["resistance_completeness"], "support_completeness": raw["support_completeness"],
    }


def select_best_level(rows, market_structure, underlying, strike_step, direction, factor_weights=None):
    """
    Evaluates every rung of the level ladder (see build_level_ladder) for
    ONE side ("support" or "resistance") via analyze_oi_cluster, and returns
    whichever rung the real data actually supports BEST (highest confidence
    among rungs with genuine tradeable_data) -- not always the nearest, not
    always the farthest, not always the same label day to day: whichever the
    real OI data actually backs. Falls back to the NEAREST rung's (possibly
    untradeable) result if none qualify, so callers always get a
    strike/cluster to report on, honestly marked not-tradeable rather than
    returning nothing.

    Returns {"label", "price", "strike", "cluster", "confidence", "ladder":
    [...every evaluated rung, for transparency/display...]}, or None if the
    ladder itself is empty (no market-structure data at all yet).
    """
    ladder = build_level_ladder(market_structure, underlying, strike_step)
    key = "resistance_candidates" if direction == "resistance" else "support_candidates"
    completeness_key = "resistance_completeness" if direction == "resistance" else "support_completeness"
    candidates = ladder[key]
    if not candidates:
        return None

    cluster_direction = "CE" if direction == "resistance" else "PE"
    evaluated = []
    for c in candidates:
        cluster = analyze_oi_cluster(rows, c["strike"], cluster_direction, strike_step, factor_weights=factor_weights)
        # Discrete pivot rungs (R1-R4/S1-S4) are single deterministic prices --
        # completeness is trivially 1.0 whenever the rung itself is available.
        # Only the DYNAMIC rung's completeness reflects how many raw
        # structural categories were actually fused into it.
        level_completeness = ladder[completeness_key] if c["label"] == "DYNAMIC" else 1.0
        confidence = compute_v3_confidence(cluster, level_completeness)
        evaluated.append({**c, "cluster": cluster, "confidence": confidence})

    tradeable = [e for e in evaluated if e["cluster"] and e["cluster"].get("tradeable_data")]
    best = max(tradeable, key=lambda e: e["confidence"]) if tradeable else evaluated[0]
    return {**best, "ladder": evaluated}


# ---------------------------------------------------------------------------
# 3. Previous-day validation (Valid / Weak / Failed)
# ---------------------------------------------------------------------------

def validate_previous_day_levels(prev_rows, prev_market_structure, prev_underlying, strike_step, factor_weights=None):
    """
    Re-runs select_best_level (the SAME ladder-based selection used live)
    against the PREVIOUS trading day's closing option chain (per the
    confirmed approach: the last logged cycle from the existing
    cycles/strikes DB tables -- no new snapshot infra) to verify yesterday's
    calculated Support/Resistance actually held up institutionally into the
    close. Buckets VALID/WEAK/FAILED from the same wall/fake scores,
    weighted thresholds not hard cutoffs (V3_VALID_*/V3_WEAK_* module
    constants -- starting values, see module docstring).
    """
    regime_weights = v3_regime_weights(prev_market_structure, prev_underlying)
    support_best = select_best_level(prev_rows, prev_market_structure, prev_underlying, strike_step, "support",
                                      factor_weights=factor_weights)
    resistance_best = select_best_level(prev_rows, prev_market_structure, prev_underlying, strike_step, "resistance",
                                         factor_weights=factor_weights)
    support_eval = support_best["cluster"] if support_best else None
    resistance_eval = resistance_best["cluster"] if resistance_best else None

    def bucket(cluster_eval):
        if not cluster_eval or not cluster_eval.get("tradeable_data"):
            return "UNKNOWN", "No closing option-chain data available for this level."
        wall, fake = cluster_eval["institutional_wall_score"], cluster_eval["fake_index"]
        if wall >= V3_VALID_WALL_MIN and fake < V3_VALID_FAKE_MAX:
            return "VALID", f"Strong institutional wall at close (wall_score={wall}, fake_index={fake})."
        if wall >= V3_WEAK_WALL_MIN and fake < V3_WEAK_FAKE_MAX:
            return "WEAK", f"Marginal wall at close (wall_score={wall}, fake_index={fake})."
        return "FAILED", f"OI had already unwound / fake-index high at close (wall_score={wall}, fake_index={fake})."

    support_status, support_reason = bucket(support_eval)
    resistance_status, resistance_reason = bucket(resistance_eval)
    return {
        "regime_weights": regime_weights,
        "support": {"status": support_status, "reason": support_reason, "cluster": support_eval,
                     "level_label": support_best["label"] if support_best else None},
        "resistance": {"status": resistance_status, "reason": resistance_reason, "cluster": resistance_eval,
                        "level_label": resistance_best["label"] if resistance_best else None},
    }


# ---------------------------------------------------------------------------
# 4. Current-day tracking (Held / Broke / Extended / Flipped)
# ---------------------------------------------------------------------------

def classify_level_outcome(level_price, level_type, today_ltps):
    """
    HELD / BROKE / FLIPPED classification of TODAY's price action against
    YESTERDAY's validated level ("Trend" input per spec: is the prior
    level still respected?). Extension is layered on top by the caller
    (detect_resistance_extend_up / detect_support_extend_down) once BROKE is
    established -- this function only looks at raw price interaction.
    """
    if level_price is None or not today_ltps:
        return "UNKNOWN"
    closed_beyond = any(
        (ltp > level_price) if level_type == "resistance" else (ltp < level_price)
        for ltp in today_ltps
    )
    if not closed_beyond:
        return "HELD"
    # FLIPPED: price broke through AND has since come back to sit on the
    # OTHER side again (role-reversal) -- judged from the last 10 readings
    # only (recent behaviour), so a level broken early and never revisited
    # doesn't get mislabeled FLIPPED much later in the day.
    recent = today_ltps[-10:]
    ever_reflipped = any(
        (ltp < level_price) if level_type == "resistance" else (ltp > level_price)
        for ltp in recent
    )
    currently_reflipped = (
        (recent[-1] < level_price) if level_type == "resistance" else (recent[-1] > level_price)
    )
    if ever_reflipped and currently_reflipped:
        return "FLIPPED"
    return "BROKE"


# ---------------------------------------------------------------------------
# 5. Extension detection
# ---------------------------------------------------------------------------

def detect_resistance_extend_up(underlying, resistance_price, row_at_resistance, row_at_next_resistance,
                                 volume_history, current_volume, candles=None, confidence=None,
                                 min_evidence=None):
    """
    Weighted evidence count (per spec checklist) for a genuine Resistance
    Extend Up -- NOT a single hard AND-gate. Returns how many of up to
    V3_EXTENSION_MAX_EVIDENCE (6) independent checks fired plus the human-
    readable detail. `min_evidence` (if given) still produces a legacy
    boolean `extending` field for convenience/logging, but callers making
    the actual hold-vs-extend decision should compare the CONTINUOUS
    `evidence_count`/V3_EXTENSION_MAX_EVIDENCE score against the hold
    probability instead (see generate_v3_signal) -- never gate on
    `extending` alone, per the weighted-scoring-not-hard-filters rule.
    """
    evidence = []
    if underlying is not None and resistance_price is not None and underlying > resistance_price:
        evidence.append("Price broke above resistance")
    if candles and check_candle_close_confirmation("resistance", "breakout", resistance_price, candles):
        evidence.append("3 candles closed above resistance (non-repaint confirmed)")
    if row_at_resistance is not None and (row_at_resistance.ce_oi_chg or 0) < 0:
        evidence.append("CE OI unwinding at the broken resistance strike")
    if row_at_next_resistance is not None and (row_at_next_resistance.ce_oi_chg or 0) > 0:
        evidence.append("Fresh CE OI buildup at the next strike out")
    expanded, ratio = compute_volume_expansion(volume_history, current_volume)
    if expanded:
        evidence.append(f"Volume expansion ({ratio}x recent average)")
    if row_at_resistance is not None and (row_at_resistance.ce_delta or 0) > 0:
        evidence.append("Positive delta confirmed")
    if confidence is not None and confidence >= V3_CONFIDENCE_TRADE_THRESHOLD:
        evidence.append(f"Strong confidence ({confidence}%)")

    threshold = min_evidence if min_evidence is not None else (V3_EXTENSION_MAX_EVIDENCE // 2)
    return {"extending": len(evidence) >= threshold, "evidence_count": len(evidence), "evidence": evidence}


def detect_support_extend_down(underlying, support_price, row_at_support, row_at_next_support,
                                volume_history, current_volume, candles=None, confidence=None,
                                min_evidence=None):
    """Mirror of detect_resistance_extend_up for the downside."""
    evidence = []
    if underlying is not None and support_price is not None and underlying < support_price:
        evidence.append("Price broke below support")
    if candles and check_candle_close_confirmation("support", "breakdown", support_price, candles):
        evidence.append("3 candles closed below support (non-repaint confirmed)")
    if row_at_support is not None and (row_at_support.pe_oi_chg or 0) < 0:
        evidence.append("PE OI unwinding at the broken support strike")
    if row_at_next_support is not None and (row_at_next_support.pe_oi_chg or 0) > 0:
        evidence.append("Fresh PE OI buildup at the next strike out")
    expanded, ratio = compute_volume_expansion(volume_history, current_volume)
    if expanded:
        evidence.append(f"Volume expansion ({ratio}x recent average)")
    if row_at_support is not None and (row_at_support.pe_delta or 0) < 0:
        evidence.append("Negative delta confirmed")
    if confidence is not None and confidence >= V3_CONFIDENCE_TRADE_THRESHOLD:
        evidence.append(f"Strong confidence ({confidence}%)")

    threshold = min_evidence if min_evidence is not None else (V3_EXTENSION_MAX_EVIDENCE // 2)
    return {"extending": len(evidence) >= threshold, "evidence_count": len(evidence), "evidence": evidence}


# ---------------------------------------------------------------------------
# 6. Entry/Target/SL + top-level signal
# ---------------------------------------------------------------------------

def compute_v3_entry_targets_sl(entry_price, level_price, next_level_price, atr, delta_approx=0.5,
                                 min_target_pct=V3_MIN_TARGET_PCT, max_sl_pct=V3_MAX_SL_PCT):
    """Reuses sr_probability_engine.compute_dynamic_targets_sl directly --
    same already-tested delta-projected structural-distance math, just fed
    V3's own dynamic level / next-level prices AND V3's own (tighter, 30-60min-
    hold-calibrated) min_target_pct/max_sl_pct -- see V3_MIN_TARGET_PCT's
    module-level comment for why the swing engine's defaults don't fit V3's
    much shorter hold. Returns (target1, target2, sl); callers use target1 as
    THE suggested target (target2 kept for parity/future use, not surfaced in
    V3's own output)."""
    return compute_dynamic_targets_sl(entry_price, level_price, next_level_price, atr, delta_approx=delta_approx,
                                       min_target_pct=min_target_pct, max_sl_pct=max_sl_pct)


def confirms_reversal_entry(direction, level_price, candles, tolerance, lookback=3):
    """
    Price-action confirmation for the "hold/reversal" trade's FINAL EXECUTION
    only (2026-08-02: moved from an early candidate filter to a final-
    execution-only gate, per explicit request -- weighted scoring alone now
    decides direction/confidence; this only decides whether to actually pull
    the trigger THIS cycle). A strong OI wall doesn't mean price is
    respecting it TODAY; this requires genuine evidence:

    one of the last `lookback` candles must have actually TESTED the level
    (its low/high came within `tolerance` of it), AND the most recent candle
    must already be closing back in the favorable direction, past the level
    (not just a wick poke).

    CE (support bounce): a recent candle tested support, AND the current
    candle is bullish (close > open) and closes ABOVE support (holding, not
    breaking down).
    PE (resistance fade): mirrored.

    Returns False (not yet confirmed -- setup stays ARMED, not executed) on
    any missing/degenerate data -- never raises, and never treats "no candle
    data" as an automatic pass.
    """
    if not candles or len(candles) < 2 or level_price is None or not tolerance:
        return False
    recent = candles[-lookback:]
    current = candles[-1]
    try:
        tested = any(
            (c.get("low") is not None and abs(c["low"] - level_price) <= tolerance)
            or (c.get("high") is not None and abs(c["high"] - level_price) <= tolerance)
            for c in recent
        )
        cur_open, cur_close = current["open"], current["close"]
    except (KeyError, TypeError):
        return False
    if not tested or cur_open is None or cur_close is None:
        return False
    if direction == "CE":
        return cur_close > cur_open and cur_close > level_price
    if direction == "PE":
        return cur_close < cur_open and cur_close < level_price
    return False


def should_pause_time_exit(direction, prev_candle, current_candle):
    """
    Candle-based override for the max-hold time-exit (added 2026-08-02 per
    request): a trade hitting its time cap purely because the clock ran out,
    while the underlying's own recent price action is STILL genuinely moving
    in its favor, is being force-closed for no real reason -- this pauses
    that forced exit for as long as the immediate candle structure keeps
    agreeing with the trade.

    Uses the PREVIOUS candle's 50% level (its high/low midpoint) as the
    structural line in the sand -- a well-known price-action reference,
    not an invented number:
      CE (long): pause if the CURRENT candle is bullish (close > open) AND
      its close is still ABOVE the previous candle's midpoint -- i.e. price
      hasn't given back more than half of the prior candle's range.
      PE (short): mirrored -- pause if the current candle is bearish AND its
      close is still BELOW the previous candle's midpoint.

    Callers MUST still enforce an absolute outer cap on top of this (see
    V3_MAX_HOLD_MINUTES_HARD_CAP in app.py/backtest.py) -- this function only
    decides whether to skip ONE cycle's time-exit, never removes the safety
    net entirely. Returns False (don't pause -- let the normal time-exit
    fire) on any missing/degenerate candle data, never raises.
    """
    if not prev_candle or not current_candle:
        return False
    try:
        prev_high, prev_low = prev_candle["high"], prev_candle["low"]
        cur_open, cur_close = current_candle["open"], current_candle["close"]
    except (KeyError, TypeError):
        return False
    if prev_high is None or prev_low is None or cur_open is None or cur_close is None:
        return False
    prev_mid = (prev_high + prev_low) / 2
    if direction == "CE":
        return cur_close > cur_open and cur_close > prev_mid
    if direction == "PE":
        return cur_close < cur_open and cur_close < prev_mid
    return False


# ---------------------------------------------------------------------------
# Adaptive weight learning -- bounded, transparent, gated on a minimum sample
# ---------------------------------------------------------------------------

def learn_adaptive_weights(trade_records, current_weights=None, min_sample=V3_WEIGHT_MIN_SAMPLE,
                            step=V3_WEIGHT_LEARN_STEP, bounds=V3_WEIGHT_BOUNDS):
    """
    Bounded, transparent weight auto-adjustment from GENUINE historical
    paper-trade outcomes (added 2026-08-02 per request) -- NOT machine
    learning, no black box: for each factor, splits resolved trades into
    "high" (this factor's entry-time score was at/above the sample median)
    vs "low" (below median), and compares win rate between the two halves.
    If "high" trades won more often, nudge that factor's weight up by
    `step` (multiplicatively); if "low" trades won more (the factor was
    actually inversely predictive on this sample, or just noise), nudge it
    down. Every nudge is clamped to `bounds` so no factor can ever run away
    to zero or dominate the score.

    trade_records: [{"exit_reason": "TARGET HIT"/"STOP LOSS"/other,
    "factors": {factor_name: score_at_entry, ...}}, ...] -- see
    v3_paper_trades.factors_json in app.py. Only TARGET HIT / STOP LOSS
    trades count as "resolved" (time-exits are directionally ambiguous,
    excluded from learning -- same reasoning update_v3_paper_trading
    already applies elsewhere).

    Requires at least `min_sample` resolved trades -- and at least
    `min_sample` trades with a given factor's score actually present --
    before adjusting THAT factor away from neutral/current. On a small
    sample this would just be curve-fitting to noise, not learning (per
    explicit instruction to avoid overfitting). Returns
    (weights_dict, diagnostics_dict) -- diagnostics always explains WHY
    each factor did or didn't move, for full transparency.
    """
    weights = dict(current_weights or V3_DEFAULT_FACTOR_WEIGHTS)
    resolved = [t for t in trade_records if t.get("exit_reason") in ("TARGET HIT", "STOP LOSS")]
    if len(resolved) < min_sample:
        return weights, {
            "adjusted": False, "sample_size": len(resolved), "min_sample": min_sample,
            "reason": f"Only {len(resolved)} resolved (TARGET HIT/STOP LOSS) trades -- need {min_sample}+ "
                      f"before adjusting any weight away from neutral/current (avoids overfitting a thin sample).",
            "adjustments": {},
        }

    adjustments = {}
    for factor in V3_DEFAULT_FACTOR_WEIGHTS:
        values = [t["factors"].get(factor) for t in resolved if t.get("factors", {}).get(factor) is not None]
        if len(values) < min_sample:
            continue
        median = sorted(values)[len(values) // 2]
        high_trades = [t for t in resolved if (t.get("factors") or {}).get(factor) is not None
                       and t["factors"][factor] >= median]
        low_trades = [t for t in resolved if (t.get("factors") or {}).get(factor) is not None
                      and t["factors"][factor] < median]
        if not high_trades or not low_trades:
            continue
        high_wr = sum(1 for t in high_trades if t["exit_reason"] == "TARGET HIT") / len(high_trades)
        low_wr = sum(1 for t in low_trades if t["exit_reason"] == "TARGET HIT") / len(low_trades)
        old_weight = weights.get(factor, 1.0)
        if high_wr > low_wr:
            weights[factor] = round(min(bounds[1], old_weight * (1 + step)), 3)
            adjustments[factor] = f"{old_weight}->{weights[factor]} (high-{factor} trades won {high_wr:.0%} vs {low_wr:.0%} for low)"
        elif low_wr > high_wr:
            weights[factor] = round(max(bounds[0], old_weight * (1 - step)), 3)
            adjustments[factor] = f"{old_weight}->{weights[factor]} (low-{factor} trades won {low_wr:.0%} vs {high_wr:.0%} for high)"

    return weights, {"adjusted": bool(adjustments), "sample_size": len(resolved),
                      "min_sample": min_sample, "adjustments": adjustments}


def generate_v3_signal(rows, underlying, market_structure, strike_step, prev_day_validation=None,
                        today_ltp_history=None, volume_history_by_key=None, expiry_date=None, now=None,
                        factor_weights=None, wall_center_history=None,
                        min_risk_reward=None, confidence_threshold=None,
                        min_target_pct=None, max_sl_pct=None):
    """
    Top-level entry point for ONE cycle. Pure/side-effect-free -- callers
    (app.py's live loop, backtest.py's replay) own all state/history buffers
    and pass in whatever's already been fetched/computed. Never raises on
    missing/degenerate data (same contract as
    scalping_engine.evaluate_scalp_candidate) -- always returns the full
    output dict, with fields honestly None/UNKNOWN when data isn't available.

    min_risk_reward/confidence_threshold/min_target_pct/max_sl_pct: override
    V3_MIN_RISK_REWARD/V3_CONFIDENCE_TRADE_THRESHOLD/V3_MIN_TARGET_PCT/
    V3_MAX_SL_PCT when given (backtest-only tuning, see app.py's
    ENGINE_PARAM_SPECS["v3"]) -- None (the default on every one) preserves
    today's exact behavior, so the live call site (app.py) stays
    byte-identical when it passes none of these.

    prev_day_validation: output of validate_previous_day_levels() for
    YESTERDAY's levels (None on the first day of collection -- outcome/
    extension fields degrade to "UNKNOWN"/not-extending).
    today_ltp_history: list of underlying LTP floats so far today (oldest
    first) -- for classify_level_outcome.
    volume_history_by_key: {(strike, "CE"/"PE"): [...]} prior-cycle volume
    readings for the resistance/support strikes (Fake-Breakout-Filter-style
    volume-expansion check in extension detection).
    factor_weights: optional learned weights (see learn_adaptive_weights) --
    defaults to neutral when omitted.
    wall_center_history: optional {"support": [...], "resistance": [...]}
    PRIOR OI-cluster-center readings (see compute_oi_cluster_center) for
    detect_wall_migration -- omit to skip migration detection this cycle.

    WEIGHTED, NOT HARD-FILTERED (see module docstring): the hold-vs-extend
    call for each side is a continuous score comparison, not a boolean gate;
    candle confirmation (confirms_reversal_entry) only gates
    `execution_confirmed`/`tradeable` at the very end, never which side is
    even considered a candidate.
    """
    now = now or dt.datetime.now()
    min_risk_reward = V3_MIN_RISK_REWARD if min_risk_reward is None else min_risk_reward
    confidence_threshold = V3_CONFIDENCE_TRADE_THRESHOLD if confidence_threshold is None else confidence_threshold
    raw_levels = compute_v3_raw_levels(market_structure, underlying)   # kept for the raw dynamic_support/dynamic_resistance display fields below
    regime_weights = v3_regime_weights(market_structure, underlying)

    # Level ladder (1st/2nd/3rd/4th steps: Pivot R1-R4/S1-S4 + DYNAMIC) --
    # picks whichever rung the real OI-cluster data actually supports best,
    # not just the single farthest DYNAMIC level (see select_best_level's
    # docstring for why: that level is very often outside the fetched
    # ATM+/-4 strike window).
    support_best = select_best_level(rows, market_structure, underlying, strike_step, "support",
                                      factor_weights=factor_weights)
    resistance_best = select_best_level(rows, market_structure, underlying, strike_step, "resistance",
                                         factor_weights=factor_weights)

    support_strike = support_best["strike"] if support_best else None
    resistance_strike = resistance_best["strike"] if resistance_best else None
    support_level_label = support_best["label"] if support_best else None
    resistance_level_label = resistance_best["label"] if resistance_best else None
    support_ladder = support_best["ladder"] if support_best else []
    resistance_ladder = resistance_best["ladder"] if resistance_best else []
    next_support = support_strike - strike_step if support_strike is not None else None
    next_resistance = resistance_strike + strike_step if resistance_strike is not None else None

    support_cluster = support_best["cluster"] if support_best else None
    resistance_cluster = resistance_best["cluster"] if resistance_best else None
    support_confidence = support_best["confidence"] if support_best else 0
    resistance_confidence = resistance_best["confidence"] if resistance_best else 0

    support_hold_pct, support_break_pct = compute_hold_break_probability(support_cluster, regime_weights)
    resistance_hold_pct, resistance_break_pct = compute_hold_break_probability(resistance_cluster, regime_weights)

    # OI Cluster Shift / Institutional Wall Migration -- continuous drift
    # read against the caller-owned recent-history buffer (prior readings
    # only). Skipped honestly (all-None) when no history buffer is supplied.
    support_center_now = compute_oi_cluster_center(rows, support_strike, "PE", strike_step)
    resistance_center_now = compute_oi_cluster_center(rows, resistance_strike, "CE", strike_step)
    wall_center_history = wall_center_history or {}
    support_wall_migration = detect_wall_migration(wall_center_history.get("support"), support_center_now, strike_step)
    resistance_wall_migration = detect_wall_migration(wall_center_history.get("resistance"), resistance_center_now, strike_step)

    row_by_strike = {r.strike: r for r in rows}
    volume_history_by_key = volume_history_by_key or {}
    today_ltp_history = today_ltp_history or []
    candles = (market_structure or {}).get("recent_candles")

    outcome = {"support": "UNKNOWN", "resistance": "UNKNOWN"}
    extension = {
        "support_extend_down": {"extending": False, "evidence_count": 0, "evidence": []},
        "resistance_extend_up": {"extending": False, "evidence_count": 0, "evidence": []},
    }

    if prev_day_validation:
        prev_support_cluster = prev_day_validation.get("support", {}).get("cluster")
        prev_resistance_cluster = prev_day_validation.get("resistance", {}).get("cluster")
        prev_support_strike = prev_support_cluster.get("center_strike") if prev_support_cluster else None
        prev_resistance_strike = prev_resistance_cluster.get("center_strike") if prev_resistance_cluster else None

        if prev_support_strike is not None:
            outcome["support"] = classify_level_outcome(prev_support_strike, "support", today_ltp_history)
            if outcome["support"] in ("BROKE", "FLIPPED"):
                row_support = row_by_strike.get(prev_support_strike)
                row_next_support = row_by_strike.get(prev_support_strike - strike_step)
                vol_hist = volume_history_by_key.get((prev_support_strike, "PE"), [])
                cur_vol = row_support.pe_vol if row_support else None
                extension["support_extend_down"] = detect_support_extend_down(
                    underlying, prev_support_strike, row_support, row_next_support,
                    vol_hist, cur_vol, candles=candles, confidence=support_confidence,
                )

        if prev_resistance_strike is not None:
            outcome["resistance"] = classify_level_outcome(prev_resistance_strike, "resistance", today_ltp_history)
            if outcome["resistance"] in ("BROKE", "FLIPPED"):
                row_resistance = row_by_strike.get(prev_resistance_strike)
                row_next_resistance = row_by_strike.get(prev_resistance_strike + strike_step)
                vol_hist = volume_history_by_key.get((prev_resistance_strike, "CE"), [])
                cur_vol = row_resistance.ce_vol if row_resistance else None
                extension["resistance_extend_up"] = detect_resistance_extend_up(
                    underlying, prev_resistance_strike, row_resistance, row_next_resistance,
                    vol_hist, cur_vol, candles=candles, confidence=resistance_confidence,
                )

    # Continuous hold-vs-extend comparison (0-100 scores, NOT a boolean gate)
    # -- whichever is higher decides the LEAN for that side; there is no
    # minimum either score has to clear beyond the overall confidence bar.
    resistance_extend_score = round(extension["resistance_extend_up"]["evidence_count"] / V3_EXTENSION_MAX_EVIDENCE * 100, 1)
    support_extend_score = round(extension["support_extend_down"]["evidence_count"] / V3_EXTENSION_MAX_EVIDENCE * 100, 1)
    resistance_hold_score = resistance_hold_pct if resistance_hold_pct is not None else 0
    support_hold_score = support_hold_pct if support_hold_pct is not None else 0

    resistance_state = "RESISTANCE_EXTEND_UP" if resistance_extend_score >= resistance_hold_score else "RESISTANCE_HOLD"
    support_state = "SUPPORT_EXTEND_DOWN" if support_extend_score >= support_hold_score else "SUPPORT_HOLD"

    # Trade decision: weighted comparison between whichever side has higher
    # confidence -- direction within each side comes from the CONTINUOUS
    # extend-vs-hold score comparison above (never a hard "extending==True"
    # or "hold>=60" gate). Both sides can independently qualify; the
    # higher-confidence one wins.
    candidates = []
    if resistance_confidence >= confidence_threshold and resistance_strike is not None:
        if resistance_extend_score >= resistance_hold_score:
            candidates.append({
                "direction": "CE", "confidence": resistance_confidence, "level": resistance_strike,
                "next_level": next_resistance, "side": "resistance", "lean": "extend",
                "reason": f"Resistance extend-score {resistance_extend_score:.0f} >= hold-score "
                          f"{resistance_hold_score:.0f} -- weighted lean: breakout continuation.",
            })
        else:
            candidates.append({
                "direction": "PE", "confidence": resistance_confidence, "level": resistance_strike,
                "next_level": support_strike, "side": "resistance", "lean": "hold",
                "reason": f"Resistance hold-score {resistance_hold_score:.0f} > extend-score "
                          f"{resistance_extend_score:.0f} -- weighted lean: fade/reversal off resistance.",
            })
    if support_confidence >= confidence_threshold and support_strike is not None:
        if support_extend_score >= support_hold_score:
            candidates.append({
                "direction": "PE", "confidence": support_confidence, "level": support_strike,
                "next_level": next_support, "side": "support", "lean": "extend",
                "reason": f"Support extend-score {support_extend_score:.0f} >= hold-score "
                          f"{support_hold_score:.0f} -- weighted lean: breakdown continuation.",
            })
        else:
            candidates.append({
                "direction": "CE", "confidence": support_confidence, "level": support_strike,
                "next_level": resistance_strike, "side": "support", "lean": "hold",
                "reason": f"Support hold-score {support_hold_score:.0f} > extend-score "
                          f"{support_extend_score:.0f} -- weighted lean: bounce/reversal off support.",
            })

    trade_decision, direction, chosen_level, chosen_next, confidence, chosen_side = "NO_TRADE", None, None, None, 0, None
    reason = "No side clears the confidence threshold this cycle."
    execution_confirmed = None
    atr_for_tol = (market_structure or {}).get("atr_14")

    if candidates:
        best = max(candidates, key=lambda c: c["confidence"])
        direction, confidence, chosen_level, chosen_next, reason, chosen_side = (
            best["direction"], best["confidence"], best["level"], best["next_level"], best["reason"], best["side"])
        trade_decision = f"BUY {direction}"

        # Candle confirmation ONLY here -- final execution, never an earlier
        # filter on which side/direction was even considered (per request).
        if best["lean"] == "hold":
            level_price = resistance_best["price"] if best["side"] == "resistance" else support_best["price"]
            tol = (atr_for_tol * 0.3) if atr_for_tol else (max(1, level_price * 0.001) if level_price else None)
            execution_confirmed = confirms_reversal_entry(direction, level_price, candles, tol)
            if not execution_confirmed:
                reason = f"{reason} ARMED (weighted decision favors this trade) -- awaiting candle confirmation before execution."
        else:
            # Extension/continuation trades already require a genuine
            # candle-close confirmation as ONE of their own weighted
            # evidence items (see detect_*_extend_*) -- no separate hard
            # gate needed here.
            execution_confirmed = True

    suggested_entry = target_price = stop_loss = risk_reward = None
    if trade_decision != "NO_TRADE":
        entry_row = row_by_strike.get(chosen_level)
        entry_price = (entry_row.ce_ltp if direction == "CE" else entry_row.pe_ltp) if entry_row else None
        if not entry_price:
            trade_decision, reason = "NO_TRADE", f"{reason} -- BLOCKED: no valid premium for {chosen_level}{direction}."
        else:
            atr = (market_structure or {}).get("atr_14")
            iv_val = (entry_row.ce_iv if direction == "CE" else entry_row.pe_iv)
            delta_approx = 0.5
            if iv_val and expiry_date:
                from greeks import black_scholes_greeks, time_to_expiry_years
                T = time_to_expiry_years(expiry_date, now=now)
                g = black_scholes_greeks(underlying, chosen_level, T, iv_val, direction)
                if g.get("valid"):
                    delta_approx = abs(g["delta"])
            target_price, _, stop_loss = compute_v3_entry_targets_sl(
                entry_price, chosen_level, chosen_next, atr, delta_approx=delta_approx,
                min_target_pct=(V3_MIN_TARGET_PCT if min_target_pct is None else min_target_pct),
                max_sl_pct=(V3_MAX_SL_PCT if max_sl_pct is None else max_sl_pct))
            risk_reward, rr_ok = validate_risk_reward(entry_price, target_price, stop_loss, min_rr=min_risk_reward)
            suggested_entry = entry_price
            if not rr_ok:
                trade_decision = "NO_TRADE"
                reason = f"{reason} -- BLOCKED: R:R {risk_reward} below the {min_risk_reward} V3 minimum."

    tradeable = bool(trade_decision != "NO_TRADE" and execution_confirmed and suggested_entry is not None)

    return {
        "computed_at": now.strftime("%H:%M:%S"),
        "dynamic_support": raw_levels["dynamic_support"], "dynamic_resistance": raw_levels["dynamic_resistance"],
        "support_strike": support_strike, "resistance_strike": resistance_strike,
        "next_support": next_support, "next_resistance": next_resistance,
        "support_inputs": raw_levels["support_inputs"], "resistance_inputs": raw_levels["resistance_inputs"],
        "support_level_label": support_level_label, "resistance_level_label": resistance_level_label,
        "support_ladder": support_ladder, "resistance_ladder": resistance_ladder,
        "support_cluster": support_cluster, "resistance_cluster": resistance_cluster,
        "support_hold_probability": support_hold_pct, "support_break_probability": support_break_pct,
        "resistance_hold_probability": resistance_hold_pct, "resistance_break_probability": resistance_break_pct,
        "support_confidence": support_confidence, "resistance_confidence": resistance_confidence,
        "support_state": support_state, "resistance_state": resistance_state,
        "support_extend_score": support_extend_score, "resistance_extend_score": resistance_extend_score,
        "support_wall_migration": support_wall_migration, "resistance_wall_migration": resistance_wall_migration,
        "support_cluster_center": support_center_now, "resistance_cluster_center": resistance_center_now,
        "regime_weights": regime_weights,
        "previous_day_validation": prev_day_validation,
        "today_outcome": outcome,
        "resistance_extend_up": extension["resistance_extend_up"],
        "support_extend_down": extension["support_extend_down"],
        "trade_decision": trade_decision, "direction": direction,
        "execution_confirmed": execution_confirmed,
        # The ACTUAL traded strike -- NOT necessarily support_strike/resistance_strike:
        # a CE can come from resistance_strike (breakout continuation) just as much as
        # support_strike (bounce reversal), and PE mirrors that. Callers (paper-trading,
        # backtest replay) MUST use this field for entry/exit tracking, never infer the
        # strike from direction + support_strike/resistance_strike (that inference is
        # wrong for exactly the continuation cases -- found during 2026-08-01 tuning).
        "strike": chosen_level, "chosen_side": chosen_side,
        "suggested_entry": suggested_entry, "target": target_price, "stop_loss": stop_loss,
        "risk_reward": risk_reward, "confidence": confidence, "reason": reason,
        "tradeable": tradeable,
    }
