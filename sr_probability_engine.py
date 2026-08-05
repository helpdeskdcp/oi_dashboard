#!/usr/bin/env python3
"""
sr_probability_engine.py -- OI-Verified Dynamic Support/Resistance Breakout &
Reversal Probability Engine.

PHASE 1 of a larger spec. This implements:
  - Evidence-based Breakout/Reversal probability scoring at Resistance
  - Evidence-based Breakdown/Reversal probability scoring at Support
  - Confidence classification (LOW/MODERATE/HIGH/VERY HIGH) based on how much
    evidence was actually AVAILABLE (data completeness), not just the score
  - A simplified 4-state status: NO EDGE / WATCH / ARMED / CONFIRMED

NOT yet implemented (future phases, intentionally out of scope for this pass):
  - Full WATCH->ARMED->CONFIRMED->ACTIVE->TARGET/STOPPED/INVALIDATED state
    machine with persistent trade tracking (this is a substantial addition on
    top of the existing paper-trading engine)
  - Dynamic strike-selection scoring (liquidity/spread/OI-based candidate
    ranking across ATM/ITM/OTM) -- we currently don't have bid/ask spread data
  - Dynamic option-premium entry TRIGGER (separate from the existing signal
    engine's entry-at-current-LTP approach)
  - Separate dynamic Target1/Target2/SL engine for this specific feature
  - Formal backtesting/calibration storage for these probabilities

Every evidence category actually used below is genuinely available from our
existing data (StrikeRow OI/price classification, PCR, VWAP, ADX/regime,
recent price history). Categories the spec mentions that we DON'T have
(bid/ask spread, futures price for indices, real order-book depth) are
explicitly NOT faked -- they're just excluded from the score, and the
confidence classification reflects that lower data completeness.
"""

import datetime as dt

STATE_RANK = {"NO_EDGE": 0, "WATCH": 1, "ARMED": 2, "CONFIRMED": 3, "ACTIVE": 4,
              "TARGET_HIT": 5, "STOPPED": 5, "INVALIDATED": 5}


def _target_state_from_probability(dominant_pct):
    if dominant_pct >= 75:
        return "CONFIRMED"
    if dominant_pct >= 65:
        return "ARMED"
    if dominant_pct >= 55:
        return "WATCH"
    return "NO_EDGE"


def compute_premium_entry_trigger(premium_history, buffer_pct=0.005):
    """
    Dynamic entry trigger -- NOT the raw current LTP. Requires premium to break
    its own recent short-term high (a small buffer above it) before the setup
    is considered genuinely triggered. This is a real, if simple, momentum
    confirmation: a single tick at the current LTP could be noise; breaking
    the recent local high is more meaningful.

    IMPORTANT: this is computed PURELY from historical readings, never
    including the reading being evaluated this cycle -- otherwise the trigger
    would be self-referential (current price could never exceed a threshold
    that includes itself in the calculation).

    premium_history: list of {"ltp": float, "time": str} readings BEFORE this
    cycle's reading. Returns None if there isn't at least 2 prior readings yet.
    """
    if not premium_history or len(premium_history) < 2:
        return None
    recent_high = max(p["ltp"] for p in premium_history if p.get("ltp"))
    return round(recent_high * (1 + buffer_pct), 2)


def compute_premium_momentum(premium_history):
    """Simple momentum: current premium vs short-term average of recent readings."""
    if not premium_history or len(premium_history) < 3:
        return None
    ltps = [p["ltp"] for p in premium_history if p.get("ltp")]
    if len(ltps) < 3:
        return None
    avg_prior = sum(ltps[:-1]) / len(ltps[:-1])
    current = ltps[-1]
    return round((current - avg_prior) / avg_prior * 100, 2) if avg_prior else None


def compute_dynamic_targets_sl(entry_price, level_price, next_level_price, underlying_atr,
                                delta_approx=0.5, min_target_pct=0.15, max_sl_pct=0.05,
                                swing_level_underlying=None, underlying_price=None):
    """
    Target1/Target2/Stop-Loss for THIS engine's own ACTIVE-state trades --
    separate from (but philosophically consistent with) the main signal
    engine's target/SL calc. Uses structural distance to the next level,
    projected through an approximate delta, with sane percentage bounds so a
    thin/illiquid structural gap doesn't produce an unrealistic target.

    SL logic (revised 2026-07-21 per explicit request -- adaptive lowest-low,
    max 5% cap):
    - Adaptive structural stop: the recent swing-low (CE/bullish trades) or
      swing-high (PE/bearish trades) of the UNDERLYING -- a genuine
      market-structure stop instead of an arbitrary flat percentage --
      projected into premium-space via the delta approximation.
    - Hard 5% cap: SL can never risk more than max_sl_pct (default 5%) of
      the entry premium, no matter how far the swing-point is -- protects
      against a stale/wide swing-level producing excessive risk.
    - Final SL = whichever of the two is TIGHTER (closer to entry -- i.e.
      less risk). The 5% cap always wins if the structural stop would be
      wider than that; the structural stop is used when it's already
      naturally tighter than 5%.
    - Falls back to the flat 5% cap alone if swing-level data isn't
      available (e.g. first day of candle collection).

    Falls back to flat percentage targets if structural distance data isn't
    available (e.g. first day of candle collection).
    """
    if entry_price is None or entry_price <= 0:
        return None, None, None

    if not next_level_price or not level_price:
        target1 = round(entry_price * (1 + min_target_pct), 2)
        target2 = round(entry_price * (1 + min_target_pct * 1.8), 2)
        sl = round(entry_price * (1 - max_sl_pct), 2)
        return target1, target2, sl

    underlying_move = abs(next_level_price - level_price)
    projected_move = underlying_move * delta_approx

    target1 = round(entry_price + max(projected_move * 0.5, entry_price * min_target_pct), 2)
    target2 = round(entry_price + max(projected_move, entry_price * min_target_pct * 1.8), 2)

    sl_cap = entry_price * (1 - max_sl_pct)   # hard ceiling -- never risk more than this

    if swing_level_underlying is not None and underlying_price is not None and underlying_price != swing_level_underlying:
        swing_distance_underlying = abs(underlying_price - swing_level_underlying)
        swing_distance_premium = swing_distance_underlying * delta_approx
        sl_structural = entry_price - swing_distance_premium
        sl = max(sl_structural, sl_cap)   # tighter of the two wins (closer to entry = less risk)
    else:
        sl = sl_cap   # no swing data available -- fall back to the flat 5% cap

    sl = round(sl, 2)
    return target1, target2, sl


def validate_risk_reward(entry_price, target1, sl, min_rr=1.5):
    """Returns (rr_ratio, meets_minimum: bool). Per spec: reject/downgrade
    setups with poor reward relative to risk rather than forcing a trade."""
    if entry_price is None or target1 is None or sl is None:
        return None, False
    risk = entry_price - sl
    reward = target1 - entry_price
    if risk <= 0:
        return None, False
    rr = round(reward / risk, 2)
    return rr, rr >= min_rr


def compute_premium_ema(premium_history, period):
    """Simple EMA over the premium reading history. Returns None if there
    isn't at least `period` readings yet."""
    ltps = [p["ltp"] for p in premium_history if p.get("ltp")]
    if len(ltps) < period:
        return None
    multiplier = 2 / (period + 1)
    ema = ltps[0]
    for price in ltps[1:]:
        ema = (price - ema) * multiplier + ema
    return round(ema, 2)


def check_premium_momentum_confirmed(premium_history, fast_period=5, slow_period=10):
    """
    Smart-scalping confirmation: EMA(fast) > EMA(slow) on the option's OWN
    premium means genuine short-term rising momentum -- not just a single
    tick that happened to exceed a recent high. Combining this WITH the
    entry-trigger (breaking the recent high) means we only enter when BOTH
    (a) price just broke out AND (b) the broader short-term trend in the
    premium itself agrees -- filters out one-tick spikes that immediately
    fade back down.

    Returns (confirmed: bool or None, ema_fast, ema_slow). confirmed=None
    means insufficient history yet (neither confirmed nor rejected -- wait).
    """
    ema_fast = compute_premium_ema(premium_history, fast_period)
    ema_slow = compute_premium_ema(premium_history, slow_period)
    if ema_fast is None or ema_slow is None:
        return None, ema_fast, ema_slow
    return ema_fast > ema_slow, ema_fast, ema_slow


def classify_price_structure(history, lookback=10):
    """
    Classifies recent underlying price structure as Higher-High/Higher-Low
    (bullish continuation), Lower-High/Lower-Low (bearish continuation), or
    Mixed/Sideways -- splits the recent history window in half and compares
    each half's high/low. A real, if simple, price-structure read (not just
    OI-based).
    """
    recent = history[-lookback:] if len(history) >= lookback else history
    ltps = [h["ltp"] for h in recent if h.get("ltp")]
    if len(ltps) < 4:
        return "INSUFFICIENT_DATA"
    mid = len(ltps) // 2
    first_half, second_half = ltps[:mid], ltps[mid:]
    if not first_half or not second_half:
        return "INSUFFICIENT_DATA"
    first_high, first_low = max(first_half), min(first_half)
    second_high, second_low = max(second_half), min(second_half)
    if second_high > first_high and second_low > first_low:
        return "HIGHER_HIGH_HIGHER_LOW"
    if second_high < first_high and second_low < first_low:
        return "LOWER_HIGH_LOWER_LOW"
    return "MIXED"


def compute_institutional_entry_score(price_structure, oi_evidence_pct, vwap_aligned, regime,
                                       premium_momentum_confirmed, wall_cross_verified,
                                       liquidity_score, risk_reward_ok):
    """
    Combines ALL evidence the engine already computed into a single 0-100
    Institutional Entry Score with itemized explainability -- a summary/
    display aggregate, NOT a replacement for the state-machine + entry-
    trigger + EMA-momentum + R:R gates that actually decide whether a trade
    opens. Score represents evidence QUALITY, not a statistical probability
    of profit.
    """
    score = 0
    breakdown = {}

    if price_structure in ("HIGHER_HIGH_HIGHER_LOW", "LOWER_HIGH_LOWER_LOW"):
        score += 20
        breakdown["Price Structure"] = "Strong"
    elif price_structure == "MIXED":
        score += 8
        breakdown["Price Structure"] = "Weak"
    else:
        breakdown["Price Structure"] = "Unavailable"

    oi_pts = round((oi_evidence_pct / 100) * 25)
    score += oi_pts
    breakdown["OI Evidence"] = "Strong" if oi_evidence_pct >= 75 else ("Moderate" if oi_evidence_pct >= 55 else "Weak")

    if vwap_aligned is True:
        score += 10
        breakdown["VWAP"] = "Strong"
    elif vwap_aligned is False:
        breakdown["VWAP"] = "Against"
    else:
        breakdown["VWAP"] = "Unavailable"

    if regime == "TRENDING":
        score += 10
        breakdown["Regime"] = "Trending (favors breakout)"
    elif regime == "RANGING":
        score += 6
        breakdown["Regime"] = "Ranging (favors reversal)"
    else:
        breakdown["Regime"] = "Unclear"

    if premium_momentum_confirmed:
        score += 15
        breakdown["Premium Momentum"] = "Strong"
    else:
        breakdown["Premium Momentum"] = "Weak / Not Confirmed"

    if wall_cross_verified:
        score += 10
        breakdown["Structural Confirmation"] = "Strong (OI-wall + price-history level agree)"
    else:
        breakdown["Structural Confirmation"] = "Moderate"

    if liquidity_score is not None:
        score += round(liquidity_score * 5)
        breakdown["Liquidity"] = "Excellent" if liquidity_score >= 0.7 else ("Good" if liquidity_score >= 0.4 else "Poor")
    else:
        breakdown["Liquidity"] = "Unavailable"

    if risk_reward_ok:
        score += 5
        breakdown["Risk:Reward"] = "Acceptable"
    else:
        breakdown["Risk:Reward"] = "Poor"

    score = min(100, score)
    if score >= 95:
        tier = "EXCEPTIONAL SETUP"
    elif score >= 90:
        tier = "VERY HIGH QUALITY"
    elif score >= 80:
        tier = "HIGH QUALITY"
    elif score >= 70:
        tier = "MODERATE QUALITY"
    else:
        tier = "NO TRADE"

    return {"score": score, "tier": tier, "breakdown": breakdown}


def advance_level_state(level_eval, prev_state=None, persistence_seconds=45, deactivation_grace_seconds=30,
                         now=None, underlying=None, level_price=None, proximity_atr=None, proximity_mult=3.0):
    """
    State machine for ONE level (Resistance / Resistance Reversal / Support /
    Support Reversal): WATCH -> ARMED -> CONFIRMED, with hysteresis so the
    state doesn't flicker on every small probability wiggle (same persistence+
    grace-period philosophy as the main bias-persistence fix). A direction
    flip (breakout-leaning suddenly becomes reversal-leaning) is treated as a
    genuinely new setup -- old progress doesn't carry over.

    This function only manages NO_EDGE -> WATCH -> ARMED -> CONFIRMED. Once
    CONFIRMED, use check_structural_trigger() + advance_active_level() to
    handle the CONFIRMED -> ACTIVE -> TARGET_HIT/STOPPED/INVALIDATED lifecycle
    (those need live price tracking, not just probability).

    prev_state: the previous return value of this function (or None for the
    very first evaluation of this level for this symbol).
    """
    now = now or dt.datetime.now()
    prob_keys = [k for k in level_eval if k.endswith("_probability")]
    a_key, b_key = prob_keys[0], prob_keys[1]
    a_pct, b_pct = level_eval[a_key], level_eval[b_key]
    if a_pct >= b_pct:
        direction, dominant_pct = a_key.replace("_probability", ""), a_pct
    else:
        direction, dominant_pct = b_key.replace("_probability", ""), b_pct

    target_state = _target_state_from_probability(dominant_pct)

    distance, distance_label = None, None
    if underlying is not None and level_price is not None:
        distance = round(abs(underlying - level_price), 2)
        if proximity_atr and distance > proximity_atr * proximity_mult:
            distance_label = f"{distance} pts away from price -- too far for a structural trigger yet, monitoring only"
            if target_state == "CONFIRMED":
                # Strong OI-evidence alone doesn't make a far-away level "confirmed" --
                # that word should mean genuinely close to mattering, not just a high
                # probability score computed from OI data regardless of proximity.
                target_state = "ARMED"
        else:
            distance_label = f"{distance} pts away from price" if distance else "at current price"

    st = dict(prev_state) if prev_state else {
        "state": "NO_EDGE", "since": now, "direction": direction,
        "target_state": target_state, "target_since": now, "grace_since": None,
    }

    if st.get("direction") != direction and st["state"] not in ("ACTIVE", "TARGET_HIT", "STOPPED", "INVALIDATED"):
        # Direction flipped before this setup went ACTIVE -- fresh start, no carried-over progress.
        st = {"state": "NO_EDGE", "since": now, "direction": direction,
              "target_state": target_state, "target_since": now, "grace_since": None}

    st["distance"], st["distance_label"] = distance, distance_label

    if st["state"] in ("ACTIVE", "TARGET_HIT", "STOPPED", "INVALIDATED"):
        return st   # lifecycle handled by advance_active_level() instead

    if st.get("target_state") != target_state:
        st["target_state"] = target_state
        st["target_since"] = now
    held = (now - st["target_since"]).total_seconds()

    cur_rank, target_rank = STATE_RANK[st["state"]], STATE_RANK[target_state]

    if target_rank > cur_rank and held >= persistence_seconds:
        st.update({"state": target_state, "since": now, "grace_since": None})
    elif target_rank < cur_rank:
        if st.get("grace_since") is None:
            st["grace_since"] = now
        elif (now - st["grace_since"]).total_seconds() > deactivation_grace_seconds:
            st.update({"state": target_state, "since": now, "grace_since": None})
    else:
        st["grace_since"] = None

    return st


def check_candle_close_confirmation(level_type, direction, level_price, candles, required_closes=3):
    """
    Non-repaint signal confirmation (added 2026-07-21 per explicit request):
    requires the last `required_closes` CANDLES to have already fully closed
    beyond the level in the trigger direction -- not just the current live
    tick, which can poke through and reverse before its own candle even
    closes. This is what makes a breakout genuinely non-repainting: once 3
    candles have closed past the level, that fact never changes retroactively.

    Only applies to breakout/breakdown (a directional push through the
    level) -- reversal setups are about touching-then-rejecting, which the
    tick-based proximity/tolerance check already handles appropriately.
    """
    if not candles or len(candles) < required_closes or level_price is None:
        return False
    recent_closes = [c["close"] for c in candles[-required_closes:]]
    if level_type == "resistance" and direction == "breakout":
        return all(c > level_price for c in recent_closes)
    elif level_type == "support" and direction == "breakdown":
        return all(c < level_price for c in recent_closes)
    return True   # reversal setups: no candle-close gate, tick-based tolerance check applies instead


def check_structural_trigger(level_type, direction, level_price, underlying, tolerance):
    """
    Once CONFIRMED (probability says the setup is strong), this checks whether
    the underlying price has ACTUALLY done the thing -- probability alone
    should never activate a trade (per spec). Returns True if price has
    genuinely interacted with the level in the direction implied.

    level_type: "resistance" or "support"
    direction: "breakout"/"reversal" (resistance) or "breakdown"/"reversal" (support)
    """
    if underlying is None or level_price is None:
        return False
    if level_type == "resistance":
        if direction == "breakout":
            return underlying > level_price + tolerance * 0.3   # genuinely traded through
        else:  # reversal
            return abs(underlying - level_price) <= tolerance   # touched it, rejection setup
    else:  # support
        if direction == "breakdown":
            return underlying < level_price - tolerance * 0.3
        else:  # reversal
            return abs(underlying - level_price) <= tolerance


def compute_staged_underlying_targets(entry_underlying, atr, structural_level, direction="bullish", active_mode="conservative"):
    """
    Staged ATR-multiple targets in UNDERLYING-price terms (points, not
    option premium) -- T1=0.5xATR, T2=1.0xATR, T3=1.5xATR.

    TWO final-target variants are computed (2026-07-22, per discussion of a
    formula/example mismatch in the original proposal -- rather than guess
    which is right, both are exposed so backtest can compare them):
    - final_conservative: whichever is CLOSER between T3 and the structural
      level (the mathematically-correct reading of "Final = Min(...)") --
      more achievable, but may leave profit on the table if price genuinely
      runs to the structural level.
    - final_optimistic: the structural level itself (e.g. resistance) --
      matches the original worked example's number, more profit potential
      but less often reached.
    `active_mode` ("conservative" or "optimistic") selects which one is
    surfaced as `final` for convenience; both variants are always included
    so nothing is silently discarded.
    """
    if entry_underlying is None or atr is None or atr <= 0:
        return None
    sign = 1 if direction == "bullish" else -1
    t1 = round(entry_underlying + sign * 0.5 * atr, 2)
    t2 = round(entry_underlying + sign * 1.0 * atr, 2)
    t3 = round(entry_underlying + sign * 1.5 * atr, 2)
    if structural_level is not None:
        final_conservative = round(min(t3, structural_level), 2) if direction == "bullish" else round(max(t3, structural_level), 2)
        final_optimistic = round(structural_level, 2)
    else:
        final_conservative = final_optimistic = t3
    final = final_conservative if active_mode == "conservative" else final_optimistic
    return {
        "t1": t1, "t2": t2, "t3": t3, "structural_level": structural_level,
        "final_conservative": final_conservative, "final_optimistic": final_optimistic,
        "final": final, "active_mode": active_mode,
    }


def advance_active_level(prev_state, underlying, level_price, next_level_price, level_type, direction, tolerance):
    """
    Handles the ACTIVE -> TARGET_HIT / STOPPED / INVALIDATED lifecycle once a
    setup has genuinely triggered. Uses UNDERLYING price movement relative to
    the triggering level and the next structural level as a lightweight
    target/stop (not a full option-premium engine -- that's the existing
    signal engine's job; this tracks whether the STRUCTURAL thesis played out).
    """
    st = dict(prev_state)
    favorable = (direction in ("breakout",) and level_type == "resistance") or \
                (direction in ("breakdown",) and level_type == "support") or \
                (direction == "reversal" and level_type == "resistance" and underlying is not None and underlying < level_price) or \
                (direction == "reversal" and level_type == "support" and underlying is not None and underlying > level_price)

    if underlying is None:
        return st

    if favorable and next_level_price is not None:
        # target = the next structural level in the favorable direction
        reached_target = (
            (direction == "breakout" and underlying >= next_level_price) or
            (direction == "breakdown" and underlying <= next_level_price) or
            (direction == "reversal" and level_type == "resistance" and underlying <= next_level_price) or
            (direction == "reversal" and level_type == "support" and underlying >= next_level_price)
        )
        if reached_target:
            st["state"] = "TARGET_HIT"
            return st

    # Stop: price re-crosses back through the triggering level by more than tolerance
    invalidated = (
        (direction == "breakout" and underlying < level_price - tolerance) or
        (direction == "breakdown" and underlying > level_price + tolerance) or
        (direction == "reversal" and level_type == "resistance" and underlying > level_price + tolerance) or
        (direction == "reversal" and level_type == "support" and underlying < level_price - tolerance)
    )
    if invalidated:
        st["state"] = "STOPPED"

    return st


def compute_volume_expansion(volume_history, current_volume, expansion_mult=1.5):
    """
    Compares current option volume to a rolling average of recent readings --
    a genuine 'is this breakout backed by real volume, or just a thin/quiet
    move' check. Returns (is_expanded: bool or None, ratio: float or None).
    None means insufficient history to judge yet (not a rejection).
    """
    vols = [v for v in volume_history if v is not None and v > 0]
    if len(vols) < 3 or current_volume is None:
        return None, None
    avg_vol = sum(vols) / len(vols)
    if avg_vol <= 0:
        return None, None
    ratio = round(current_volume / avg_vol, 2)
    return ratio >= expansion_mult, ratio


def fake_breakout_filter(volume_expanded, oi_supports_direction, premium_rising, vwap_aligned):
    """
    Combines several independent 'is this breakout real' checks into one
    explicit gate, matching the spec's Fake Breakout Filter: reject a trade
    if breakout volume is weak, OI doesn't support the direction, premium
    isn't actually rising, or price is trading against VWAP. Any missing
    (None) input is treated as 'not disqualifying' (we don't reject for data
    we don't have -- only for data we DO have that says no).

    Returns (passes: bool, failed_checks: list[str]) for explainability.
    """
    failed = []
    if volume_expanded is False:
        failed.append("Volume expansion insufficient (breakout not backed by real volume)")
    if oi_supports_direction is False:
        failed.append("OI does not support this direction")
    if premium_rising is False:
        failed.append("Premium is not genuinely rising")
    if vwap_aligned is False:
        failed.append("Price is trading against VWAP")
    return len(failed) == 0, failed


def score_strike_candidates(rows, atm, strike_step, direction):
    """
    Dynamic strike-selection: scores ATM, one-strike-ITM, one-strike-OTM using
    ONLY genuinely available data (volume, OI, OI stability). We do NOT have
    bid/ask spread or order-book depth from our data sources -- that criterion
    from the spec is honestly excluded rather than faked, and reflected in a
    lower max achievable score/confidence rather than a fabricated value.

    direction: "CE" or "PE"
    Returns a ranked list of {"strike", "score", "reason", "liquidity_ok"}.
    """
    if direction == "CE":
        candidates = [atm, atm - strike_step, atm + strike_step]   # ATM, ITM, OTM for a call
    else:
        candidates = [atm, atm + strike_step, atm - strike_step]   # ATM, ITM, OTM for a put

    row_by_strike = {r.strike: r for r in rows}
    scored = []
    all_vols = [
        (r.ce_vol if direction == "CE" else r.pe_vol) for r in rows
        if (r.ce_vol if direction == "CE" else r.pe_vol)
    ]
    max_vol = max(all_vols) if all_vols else 1

    for strike in candidates:
        row = row_by_strike.get(strike)
        if not row:
            continue
        vol = row.ce_vol if direction == "CE" else row.pe_vol
        oi = row.ce_oi if direction == "CE" else row.pe_oi
        oi_chg = row.ce_oi_chg if direction == "CE" else row.pe_oi_chg
        ltp = row.ce_ltp if direction == "CE" else row.pe_ltp

        liquidity_ok = vol > 0 and oi > 0 and ltp and ltp > 0
        vol_score = min(1.0, vol / max_vol) if max_vol else 0
        oi_score = min(1.0, oi / 500000) if oi else 0   # soft cap, not a hard fabricated threshold
        stability_score = 0.5 if oi_chg == 0 else min(1.0, 0.5 + abs(oi_chg) / max(oi, 1))

        score = round((vol_score * 0.45 + oi_score * 0.35 + stability_score * 0.20), 3) if liquidity_ok else 0.0
        reason = "liquid" if liquidity_ok else "illiquid/stale -- rejected"

        scored.append({"strike": strike, "score": score, "ltp": ltp, "volume": vol, "oi": oi,
                        "liquidity_ok": liquidity_ok, "reason": reason})

    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored



def _nearest_row(rows, target_strike):
    if not rows:
        return None
    return min(rows, key=lambda r: abs(r.strike - target_strike))


def _extract_history_ltps(history):
    """Precomputes the valid LTP values from a history buffer ONCE -- used by
    _count_touches, which would otherwise re-extract the same values
    redundantly once per level (4x per cycle) since the same `history` is
    checked against 4 different level prices."""
    if not history:
        return []
    return [h["ltp"] for h in history if h.get("ltp") is not None]


def _count_breaks(ltps, level_price, level_type):
    """
    How many DISTINCT times has price genuinely crossed beyond this level
    (not just touched/tested it)? Counts transitions from inside-range to
    beyond-level as discrete break-events -- a single sustained move beyond
    the level counts as ONE break, not once per tick while beyond it.
    level_type: "resistance" (break = price closes above) or "support"
    (break = price closes below).
    """
    if not ltps or not level_price:
        return 0
    breaks = 0
    was_beyond = False
    for ltp in ltps:
        beyond = (ltp > level_price) if level_type == "resistance" else (ltp < level_price)
        if beyond and not was_beyond:
            breaks += 1
        was_beyond = beyond
    return breaks


def _count_touches(ltps, level_price, tolerance):
    """How many times in recent history has price come within `tolerance` of
    this level? A crude but genuine proxy for 'repeated test' -- more touches
    without a sustained break through it is real reversal evidence.
    `ltps`: a plain list of floats (see _extract_history_ltps) -- NOT the raw
    history-of-dicts, to avoid redundant extraction across multiple levels."""
    if not ltps or not level_price:
        return 0
    return sum(1 for ltp in ltps if abs(ltp - level_price) <= tolerance)


def _confidence_label(available_count, total_possible, pct_gap):
    """Confidence = data completeness AND how decisive the score is (a 51/49
    split is low-confidence even with complete data; a 90/10 split with only
    half the evidence available is still only moderate).

    pct_gap: abs(breakout_pct - reversal_pct), i.e. already normalized to a
    0-100 percentage-point gap (NOT the raw weighted score difference, whose
    magnitude depends on how many evidence categories fired this cycle and
    can run past +/-1 -- that would silently over/under-qualify decisiveness
    and let `combined` below exceed 1)."""
    completeness = available_count / total_possible if total_possible else 0
    decisiveness = max(0.0, min(1.0, abs(pct_gap) / 100))   # 0 = tied 50/50, 1 = maximally decisive 100/0
    combined = (completeness * 0.5) + (decisiveness * 0.5)
    if combined >= 0.75:
        return "VERY HIGH"
    if combined >= 0.55:
        return "HIGH"
    if combined >= 0.35:
        return "MODERATE"
    return "LOW"


def _status_from_probability(prob_pct):
    if prob_pct >= 75:
        return "CONFIRMED"
    if prob_pct >= 65:
        return "ARMED"
    if prob_pct >= 55:
        return "WATCH"
    return "NO EDGE"


def evaluate_resistance(level_price, rows, atm, pcr, market_structure, history, underlying,
                         wall_cross_verified=None, tolerance=None, history_ltps=None):
    """Returns breakout vs reversal evidence/probability at a resistance level.
    history_ltps: optional precomputed _extract_history_ltps(history) -- pass
    this in when calling for multiple levels against the SAME history (as
    build_sr_probability_table does) to avoid redundant re-extraction."""
    row = _nearest_row(rows, level_price)
    ms = market_structure or {}
    regime = ms.get("regime")
    vwap = ms.get("vwap")
    atr = ms.get("atr_14")
    tol = tolerance if tolerance is not None else (atr * 0.3 if atr else max(1, level_price * 0.001))

    breakout_score, reversal_score = 0.0, 0.0
    available = 0
    total_possible = 10

    if row:
        available += 1
        if row.ce_signal == "Short Covering":
            breakout_score += 0.25
        elif row.ce_signal == "Long Buildup":
            breakout_score += 0.15
        elif row.ce_signal == "Short Buildup":
            reversal_score += 0.25

    if row:
        available += 1
        if row.pe_signal == "Short Buildup":
            breakout_score += 0.15   # fresh put writing below resistance -- bullish context
        elif row.pe_signal in ("Short Covering", "Long Unwinding"):
            reversal_score += 0.10   # put support fading -- less bullish backing

    # Direct ATM-strike rule: spot's position relative to ATM + raw ATM Call OI
    # direction. Spot>ATM+Call-unwinding = bullish (supports breakout); spot<ATM+
    # fresh Call writing = bearish (supports rejection/reversal at resistance).
    atm_row = next((r for r in rows if r.strike == atm), None)
    if atm_row and underlying is not None and atm_row.ce_oi_chg is not None:
        available += 1
        if underlying > atm and atm_row.ce_oi_chg < 0:
            breakout_score += 0.15
        elif underlying < atm and atm_row.ce_oi_chg > 0:
            reversal_score += 0.15

    # Mother Candle + Inside Bar confirmation: a genuine close-confirmed
    # breakout from a consolidation pattern is real price-action evidence,
    # distinct from OI. Wick-only pokes don't count (already filtered out by
    # detect_mother_candle itself).
    mother = ms.get("mother_candle") or {}
    if mother.get("found"):
        available += 1
        if mother.get("breakout_confirmed") and mother.get("breakout_direction") == "bullish":
            breakout_score += 0.15
        elif mother.get("breakout_confirmed") and mother.get("breakout_direction") == "bearish":
            reversal_score += 0.10

    # Liquidity Sweep + Reclaim: a bullish sweep (stop-hunt below PDL then
    # reclaim) is genuine shakeout evidence -- often precedes upside.
    sweep = ms.get("liquidity_sweep") or {}
    if sweep.get("swept"):
        available += 1
        if sweep.get("swept") == "bullish" and sweep.get("reclaimed"):
            breakout_score += 0.10
        elif sweep.get("swept") == "bearish" and sweep.get("reclaimed"):
            reversal_score += 0.08

    # PCR (Put-Call Ratio): high PCR (excess put-writing/buying) leans bullish
    # -- supports a breakout through resistance. Low PCR (excess call activity)
    # leans bearish -- supports rejection/reversal at resistance. Same
    # thresholds as the reference engine's detect_bias() for consistency.
    if pcr is not None:   # PCR==0.0 (no put OI) is a real, maximally-bearish
                            # reading -- a truthy check would silently skip it.
        available += 1
        if pcr > 1.5:
            breakout_score += 0.08
        elif pcr < 0.7:
            reversal_score += 0.08
        # 0.7-1.5 is the explicit neutral zone -- no evidence contribution

    if vwap and underlying:
        available += 1
        if underlying > vwap:
            breakout_score += 0.10
        else:
            reversal_score += 0.10

    if regime:
        available += 1
        if regime == "TRENDING":
            breakout_score += 0.15
        elif regime == "RANGING":
            reversal_score += 0.15

    if wall_cross_verified is not None:
        available += 1
        if wall_cross_verified:
            breakout_score += 0.15

    touches = _count_touches(history_ltps if history_ltps is not None else _extract_history_ltps(history), level_price, tol)
    if history:
        available += 1
        if touches >= 3:
            reversal_score += min(0.20, touches * 0.05)   # repeated rejection without a clean break

    total = breakout_score + reversal_score
    if total <= 0:
        breakout_pct, reversal_pct = 50, 50
    else:
        breakout_pct = round(breakout_score / total * 100)
        reversal_pct = 100 - breakout_pct

    confidence = _confidence_label(available, total_possible, abs(breakout_pct - reversal_pct))
    dominant_pct = max(breakout_pct, reversal_pct)
    status = _status_from_probability(dominant_pct)
    action = "BUY CE" if breakout_pct > reversal_pct else "BUY PE"
    ltps_for_breaks = history_ltps if history_ltps is not None else _extract_history_ltps(history)
    broken_count = _count_breaks(ltps_for_breaks, level_price, "resistance")

    return {
        "level": "Resistance", "value": round(level_price, 2),
        "breakout_probability": breakout_pct, "reversal_probability": reversal_pct,
        "confidence": confidence, "status": status, "action_candidate": action,
        "touches_today": touches, "broken_count": broken_count, "data_completeness": f"{available}/{total_possible}",
    }


def evaluate_support(level_price, rows, atm, pcr, market_structure, history, underlying,
                      wall_cross_verified=None, tolerance=None, history_ltps=None):
    """Returns breakdown vs reversal evidence/probability at a support level (mirror of resistance)."""
    row = _nearest_row(rows, level_price)
    ms = market_structure or {}
    regime = ms.get("regime")
    vwap = ms.get("vwap")
    atr = ms.get("atr_14")
    tol = tolerance if tolerance is not None else (atr * 0.3 if atr else max(1, level_price * 0.001))

    breakdown_score, reversal_score = 0.0, 0.0
    available = 0
    total_possible = 10

    if row:
        available += 1
        if row.pe_signal == "Short Covering":
            breakdown_score += 0.25
        elif row.pe_signal == "Long Buildup":
            breakdown_score += 0.15
        elif row.pe_signal == "Short Buildup":
            reversal_score += 0.25

    if row:
        available += 1
        if row.ce_signal == "Short Buildup":
            breakdown_score += 0.15   # fresh call writing above support -- bearish context
        elif row.ce_signal in ("Short Covering", "Long Unwinding"):
            reversal_score += 0.10

    # Direct ATM-strike rule (mirror of evaluate_resistance): spot<ATM+fresh Call
    # writing = bearish (supports breakdown); spot>ATM+Call unwinding = bullish
    # (supports reversal/bounce off support).
    atm_row = next((r for r in rows if r.strike == atm), None)
    if atm_row and underlying is not None and atm_row.ce_oi_chg is not None:
        available += 1
        if underlying < atm and atm_row.ce_oi_chg > 0:
            breakdown_score += 0.15
        elif underlying > atm and atm_row.ce_oi_chg < 0:
            reversal_score += 0.15

    mother = ms.get("mother_candle") or {}
    if mother.get("found"):
        available += 1
        if mother.get("breakout_confirmed") and mother.get("breakout_direction") == "bearish":
            breakdown_score += 0.15
        elif mother.get("breakout_confirmed") and mother.get("breakout_direction") == "bullish":
            reversal_score += 0.10

    # Liquidity Sweep + Reclaim (mirror): a bearish sweep (stop-hunt above
    # PDH then reclaim back below) is genuine shakeout evidence for downside.
    sweep = ms.get("liquidity_sweep") or {}
    if sweep.get("swept"):
        available += 1
        if sweep.get("swept") == "bearish" and sweep.get("reclaimed"):
            breakdown_score += 0.10
        elif sweep.get("swept") == "bullish" and sweep.get("reclaimed"):
            reversal_score += 0.08

    # PCR (mirror): low PCR (excess call activity) leans bearish -- supports
    # a breakdown through support. High PCR (excess put activity) leans
    # bullish -- supports a reversal/bounce at support.
    if pcr is not None:   # PCR==0.0 is a real (maximally call-heavy) reading
        available += 1
        if pcr < 0.7:
            breakdown_score += 0.08
        elif pcr > 1.5:
            reversal_score += 0.08

    if vwap and underlying:
        available += 1
        if underlying < vwap:
            breakdown_score += 0.10
        else:
            reversal_score += 0.10

    if regime:
        available += 1
        if regime == "TRENDING":
            breakdown_score += 0.15
        elif regime == "RANGING":
            reversal_score += 0.15

    if wall_cross_verified is not None:
        available += 1
        if wall_cross_verified:
            breakdown_score += 0.15

    touches = _count_touches(history_ltps if history_ltps is not None else _extract_history_ltps(history), level_price, tol)
    if history:
        available += 1
        if touches >= 3:
            reversal_score += min(0.20, touches * 0.05)

    total = breakdown_score + reversal_score
    if total <= 0:
        breakdown_pct, reversal_pct = 50, 50
    else:
        breakdown_pct = round(breakdown_score / total * 100)
        reversal_pct = 100 - breakdown_pct

    confidence = _confidence_label(available, total_possible, abs(breakdown_pct - reversal_pct))
    dominant_pct = max(breakdown_pct, reversal_pct)
    status = _status_from_probability(dominant_pct)
    action = "BUY PE" if breakdown_pct > reversal_pct else "BUY CE"
    ltps_for_breaks = history_ltps if history_ltps is not None else _extract_history_ltps(history)
    broken_count = _count_breaks(ltps_for_breaks, level_price, "support")

    return {
        "level": "Support", "value": round(level_price, 2),
        "breakdown_probability": breakdown_pct, "reversal_probability": reversal_pct,
        "confidence": confidence, "status": status, "action_candidate": action,
        "touches_today": touches, "broken_count": broken_count, "data_completeness": f"{available}/{total_possible}",
    }


def build_sr_probability_table(rows, atm, pcr, market_structure, history, underlying,
                                custom_levels, resistance_wall_confirmed=None, support_wall_confirmed=None):
    """Top-level entry point: evaluates all 4 levels (Resistance, Resistance
    Reversal, Support, Support Reversal) from custom_range_levels. Returns
    None if custom_levels isn't available yet (e.g. first day of collection)."""
    if not custom_levels:
        return None
    history_ltps = _extract_history_ltps(history)   # computed ONCE, reused across all 4 evaluate_* calls below
                                                       # (was previously re-extracted redundantly 4x per cycle)
    return {
        "resistance": evaluate_resistance(custom_levels["resistance"], rows, atm, pcr, market_structure,
                                            history, underlying, wall_cross_verified=resistance_wall_confirmed,
                                            history_ltps=history_ltps),
        "resistance_reversal": evaluate_resistance(custom_levels["resistance_reversal"], rows, atm, pcr,
                                                     market_structure, history, underlying, history_ltps=history_ltps),
        "support": evaluate_support(custom_levels["support"], rows, atm, pcr, market_structure,
                                      history, underlying, wall_cross_verified=support_wall_confirmed,
                                      history_ltps=history_ltps),
        "support_reversal": evaluate_support(custom_levels["support_reversal"], rows, atm, pcr,
                                               market_structure, history, underlying, history_ltps=history_ltps),
        "computed_at": dt.datetime.now().strftime("%H:%M:%S"),
    }
