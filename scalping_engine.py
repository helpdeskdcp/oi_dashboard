#!/usr/bin/env python3
"""
scalping_engine.py -- Dedicated fast-timeframe option-scalping signal engine.

Pure, side-effect-free (same contract as oi_engine.py) -- callers pass in
whatever's ALREADY been fetched/computed this cycle (rows, atm, underlying,
market_structure, premium/volume history buffers). No network/DB calls here,
so evaluating this every cycle is cheap and adds zero extra API-rate-limit
risk on top of the existing live loop.

REUSES the already-tested premium-momentum/volume/entry-trigger building
blocks from sr_probability_engine.py (compute_premium_entry_trigger,
check_premium_momentum_confirmed, compute_volume_expansion,
fake_breakout_filter, score_strike_candidates) rather than reimplementing
them -- see oi_engine.py's own docstring for why duplicating this kind of
logic across the codebase is a hard no (live vs backtest drift risk applies
here too: a scalp engine that scores momentum differently from what's
already validated in test_engine.py would be untested by construction).

DIFFERENT PHILOSOPHY FROM the swing-oriented engines (oi_engine.generate_signal,
sr_probability_engine's WATCH->ARMED->CONFIRMED->ACTIVE state machine):
- The swing engines only look for entries near a structural S/R level and
  hold up to MAX_HOLD_MINUTES (default 30) for a full R:R move to play out.
- This engine evaluates EVERY cycle, independent of proximity to any level --
  the entire thesis is "the option's own premium is accelerating RIGHT NOW,
  on real volume", not "price is near a level that might react". It targets
  a small, fast capture with a MUCH shorter max hold and a tight,
  volatility-scaled stop.

This module is advisory/read-only: it produces a signal dict, nothing more.
Wiring it up to actually place (paper or real) trades is a separate,
deliberate decision left to the caller -- this keeps a brand-new, so-far
un-backtested strategy from silently starting to trade real capital.
"""

from greeks import black_scholes_greeks, time_to_expiry_years
from sr_probability_engine import (
    compute_premium_entry_trigger, check_premium_momentum_confirmed,
    compute_volume_expansion, fake_breakout_filter, score_strike_candidates,
)

# -- Scalp-specific tuning (deliberately different from the swing engines') --
SCALP_MAX_HOLD_MINUTES = 6          # vs MAX_HOLD_MINUTES=30 for the swing engine
SCALP_MIN_TARGET_PCT = 0.06         # 6% of premium -- small, quick capture
SCALP_MAX_SL_PCT = 0.035            # 3.5% -- tight, since the hold is short
SCALP_MIN_SL_PCT = 0.015            # floor -- see compute_scalp_targets_sl's docstring
SCALP_MIN_RISK_REWARD = 1.2         # lower bar than the swing engine's 1.5 -- scalps trade frequency for size
SCALP_VOLUME_EXPANSION_MULT = 1.3
SCALP_EMA_FAST, SCALP_EMA_SLOW = 3, 7   # much shorter EMA periods than the swing engine's 5/10 -- scalping needs to react in a handful of ticks, not a couple dozen
PREMIUM_HISTORY_MAXLEN = 30
SCALP_COOLDOWN_MINUTES_AFTER_SL = 3   # short, scalp-scaled cooldown (vs the swing engine's 10min) -- long enough to avoid re-entering into the same whipsaw, short enough to not sit out a still-fast-moving tape


def _clamp(x, lo=-100.0, hi=100.0):
    return max(lo, min(hi, x))


def volatility_regime_multiplier(market_structure):
    """
    Scales target/SL distance by current volatility/trend strength (ATR%,
    regime, ADX) -- a scalp in a genuinely fast-moving, trending tape can
    reach (and needs) a bit more room than one in a dead-flat range, where a
    fixed-percentage target would rarely be hit before the short hold-time
    cap forces a time-exit. Returns a multiplier clamped to [0.6, 1.6] so a
    missing/degenerate ADX or an extreme regime reading never produces an
    absurd target/SL blowout.
    """
    ms = market_structure or {}
    mult = 1.0
    regime = ms.get("regime")
    if regime == "TRENDING":
        mult *= 1.25
    elif regime == "RANGING":
        mult *= 0.85
    adx = ms.get("adx")
    if adx is not None:
        if adx >= 35:
            mult *= 1.15
        elif adx < 15:
            mult *= 0.9
    return max(0.6, min(1.6, mult))


def _delta_for_strike(row, direction, underlying, expiry_date, now=None):
    # expiry_date must be a datetime.date (e.g. app.py's parse_expiry() output),
    # NOT a raw broker date string -- greeks.time_to_expiry_years requires it.
    """Black-Scholes |delta| for this strike, falling back to the same 0.5
    flat estimate greeks.py itself falls back to on degenerate inputs --
    used to project an underlying-space move into premium-space."""
    if not expiry_date:
        return 0.5
    time_years = time_to_expiry_years(expiry_date, now=now)
    iv = row.ce_iv if direction == "CE" else row.pe_iv
    result = black_scholes_greeks(underlying, row.strike, time_years, iv, direction)
    return abs(result["delta"]) or 0.5


def compute_scalp_targets_sl(entry_price, underlying, atr, delta_used, regime_mult,
                              min_target_pct=SCALP_MIN_TARGET_PCT, max_sl_pct=SCALP_MAX_SL_PCT,
                              min_sl_pct=SCALP_MIN_SL_PCT):
    """
    Scalp target/SL in PREMIUM space, delta-projected from a SMALL
    underlying-space move (a fraction of ATR, not a full swing-level
    distance -- there's no "next level" concept here, just "is this moving").
    Falls back to a flat-percentage-of-underlying move when ATR isn't
    available yet, same degrade-gracefully pattern as
    sr_probability_engine.compute_dynamic_targets_sl.

    SL is clamped to [min_sl_pct, max_sl_pct] of entry -- the ATR-fraction
    "structural" distance is only a rough near-term-noise estimate (unlike
    the SR engine's genuine swing-level structural stop), so in low-ATR
    moments it can shrink to a fraction of a percent: well inside normal
    premium tick noise, not a real adverse move. Backtesting found 45/72 SL
    exits landing under 1% of entry (median 0.52%, some under 0.1%), many
    stopped out within seconds -- min_sl_pct floors that. max_sl_pct still
    caps the wide side so risk can't run unbounded either.
    """
    underlying_move_for_target = (atr * 0.20 * regime_mult) if atr else (underlying * 0.0012 * regime_mult)
    target_price = round(entry_price + max(underlying_move_for_target * delta_used, entry_price * min_target_pct), 2)

    underlying_move_for_sl = (atr * 0.12 * regime_mult) if atr else (underlying * 0.0008 * regime_mult)
    sl_distance_structural = underlying_move_for_sl * delta_used
    sl_distance = max(min(sl_distance_structural, entry_price * max_sl_pct), entry_price * min_sl_pct)
    sl_price = round(max(entry_price - sl_distance, 0.05), 2)   # never below 5 paise

    return target_price, sl_price


def evaluate_scalp_candidate(row, direction, underlying, market_structure, premium_history,
                              volume_history, expiry_date, now=None,
                              min_risk_reward=SCALP_MIN_RISK_REWARD,
                              volume_expansion_mult=SCALP_VOLUME_EXPANSION_MULT):
    """
    Evaluates ONE strike/direction candidate for a scalp entry.

    row: StrikeRow for the candidate strike.
    direction: "CE" or "PE".
    premium_history / volume_history: PRIOR readings only for this exact
    strike+direction, EXCLUDING the current cycle (same contract as
    sr_probability_engine.compute_premium_entry_trigger -- callers append
    the current reading AFTER calling this, never before).
    expiry_date: a datetime.date (e.g. app.py's parse_expiry() output), not
    a raw broker date string.

    Returns a dict, always -- {"tradeable": False, "reason": ...} on any
    disqualification, {"tradeable": True, ...entry/target/sl/rr...} on a
    genuine signal. Never raises on missing/degenerate data.
    """
    entry_price = row.ce_ltp if direction == "CE" else row.pe_ltp
    if not entry_price or entry_price <= 0:
        return {"tradeable": False, "direction": direction, "strike": row.strike,
                "reason": "No valid current premium for this strike."}

    current_volume = row.ce_vol if direction == "CE" else row.pe_vol
    oi_chg = row.ce_oi_chg if direction == "CE" else row.pe_oi_chg

    # 1. Entry trigger: premium must break its own recent local high (reused,
    #    tested logic -- same function the swing engine uses).
    trigger = compute_premium_entry_trigger(premium_history)
    if trigger is None:
        return {"tradeable": False, "direction": direction, "strike": row.strike,
                "reason": "Not enough premium history yet (needs a few prior cycles)."}
    if entry_price < trigger:
        return {"tradeable": False, "direction": direction, "strike": row.strike,
                "reason": f"Premium {entry_price} hasn't broken its recent-high trigger {trigger} yet.",
                "entry_trigger": trigger}

    # 2. Momentum confirmation: fast/slow EMA on the premium itself, much
    #    shorter periods than the swing engine so a scalp can react within a
    #    handful of cycles instead of needing 10+ readings to build up.
    ema_confirmed, ema_fast, ema_slow = check_premium_momentum_confirmed(
        premium_history, SCALP_EMA_FAST, SCALP_EMA_SLOW
    )

    # 3. Volume expansion + OI direction + VWAP alignment -- the same Fake
    #    Breakout Filter gate the swing engine uses, reused as-is.
    volume_expanded, volume_ratio = compute_volume_expansion(volume_history, current_volume, volume_expansion_mult)
    oi_supports_direction = None
    if oi_chg is not None:
        oi_supports_direction = oi_chg > 0   # fresh OI buildup on the side we'd be buying
    vwap = (market_structure or {}).get("vwap")
    vwap_aligned = None
    if vwap and underlying:
        vwap_aligned = (underlying >= vwap) if direction == "CE" else (underlying <= vwap)

    passes, failed_checks = fake_breakout_filter(
        volume_expanded=volume_expanded, oi_supports_direction=oi_supports_direction,
        premium_rising=ema_confirmed, vwap_aligned=vwap_aligned,
    )
    if not passes:
        return {"tradeable": False, "direction": direction, "strike": row.strike,
                "reason": "Fake-breakout filter blocked entry: " + "; ".join(failed_checks),
                "entry_trigger": trigger, "volume_ratio": volume_ratio}

    # 4. Scalp-specific target/SL: small, delta-projected, volatility-scaled,
    #    with a MUCH shorter max-hold than the swing engine.
    regime_mult = volatility_regime_multiplier(market_structure)
    atr = (market_structure or {}).get("atr_14")
    delta_used = _delta_for_strike(row, direction, underlying, expiry_date, now=now)
    target_price, sl_price = compute_scalp_targets_sl(entry_price, underlying, atr, delta_used, regime_mult)

    risk = entry_price - sl_price
    reward = target_price - entry_price
    risk_reward = round(reward / risk, 2) if risk > 0 else None
    if risk_reward is None or risk_reward < min_risk_reward:
        return {"tradeable": False, "direction": direction, "strike": row.strike,
                "reason": f"Risk:Reward {risk_reward} below the {min_risk_reward} scalp minimum.",
                "entry_price": entry_price, "target_price": target_price, "sl_price": sl_price,
                "risk_reward": risk_reward}

    return {
        "tradeable": True, "direction": direction, "strike": row.strike,
        "entry_price": entry_price, "target_price": target_price, "sl_price": sl_price,
        "risk_reward": risk_reward, "max_hold_minutes": SCALP_MAX_HOLD_MINUTES,
        "entry_trigger": trigger, "ema_fast": ema_fast, "ema_slow": ema_slow,
        "volume_ratio": volume_ratio, "delta_used": round(delta_used, 3),
        "regime_multiplier": round(regime_mult, 2),
        "reason": "Premium broke its recent high with EMA momentum + volume/OI/VWAP confirmation -- fast scalp entry.",
    }


def generate_scalp_signal(rows, atm, strike_step, underlying, market_structure,
                           premium_history_by_key, volume_history_by_key, expiry_date, now=None):
    """
    Top-level entry point for one cycle: picks the best candidate strike for
    EACH direction (reusing score_strike_candidates' ATM/ITM/OTM liquidity
    ranking) and evaluates both, returning whichever genuinely qualifies (or
    both, if both do -- unusual but not impossible; caller decides how to
    handle that).

    premium_history_by_key / volume_history_by_key: {(strike, direction):
    [...]} -- caller-owned buffers, PRIOR readings only (see
    evaluate_scalp_candidate). This module never mutates them.

    Returns {"CE": <result dict or None>, "PE": <result dict or None>}.
    """
    row_by_strike = {r.strike: r for r in rows}
    out = {"CE": None, "PE": None}
    for direction in ("CE", "PE"):
        candidates = score_strike_candidates(rows, atm, strike_step, direction)
        liquid_candidates = [c for c in candidates if c.get("liquidity_ok")]
        if not liquid_candidates:
            out[direction] = {"tradeable": False, "direction": direction,
                               "reason": "No liquid strike candidate this cycle."}
            continue
        strike = liquid_candidates[0]["strike"]
        row = row_by_strike.get(strike)
        if not row:
            out[direction] = {"tradeable": False, "direction": direction, "strike": strike,
                               "reason": "Selected strike missing from this cycle's rows."}
            continue
        key = (strike, direction)
        result = evaluate_scalp_candidate(
            row, direction, underlying, market_structure,
            premium_history_by_key.get(key, []), volume_history_by_key.get(key, []),
            expiry_date, now=now,
        )
        out[direction] = result
    return out
