#!/usr/bin/env python3
"""
market_structure.py -- Candle-based market structure: ATR, swing high/low,
PDH/PDL/PDC, opening range, VWAP, classical pivots.

FOUNDATION PHASE ONLY: this module computes and exposes structure levels on
the dashboard. It does NOT yet drive trading signals (no breakout buffer,
no candle-close confirmation gating entries) -- that's the next phase, once
these numbers have been visually verified against a real trading day.

Candles come from Angel One SmartAPI's getCandleData (real broker OHLCV,
not reconstructed from our own sparse tick snapshots -- far more accurate).
"""

import datetime as dt


def calc_adx(candles: list, period: int = 14):
    """
    Wilder's ADX/+DI/-DI. Returns (plus_di, minus_di, adx) -- any can be None if
    there isn't enough candle history yet. ADX measures TREND STRENGTH (not
    direction): high ADX = market is trending (breakout-continuation logic
    applies), low ADX = market is range-bound (mean-reversion/fade logic
    applies). This is the standard institutional way to decide which of the
    two strategies is appropriate right now, rather than guessing.
    """
    if len(candles) < period * 2:
        return None, None, None

    trs, plus_dms, minus_dms = [], [], []
    for i in range(1, len(candles)):
        high, low = candles[i]["high"], candles[i]["low"]
        prev_high, prev_low, prev_close = candles[i - 1]["high"], candles[i - 1]["low"], candles[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
        up_move, down_move = high - prev_high, prev_low - low
        plus_dms.append(up_move if (up_move > down_move and up_move > 0) else 0)
        minus_dms.append(down_move if (down_move > up_move and down_move > 0) else 0)

    if len(trs) < period:
        return None, None, None

    def wilder_smooth(values, period):
        smoothed = [sum(values[:period])]
        for v in values[period:]:
            smoothed.append(smoothed[-1] - smoothed[-1] / period + v)
        return smoothed

    smoothed_tr = wilder_smooth(trs, period)
    smoothed_plus_dm = wilder_smooth(plus_dms, period)
    smoothed_minus_dm = wilder_smooth(minus_dms, period)

    plus_dis = [100 * pdm / tr if tr > 0 else 0 for pdm, tr in zip(smoothed_plus_dm, smoothed_tr)]
    minus_dis = [100 * mdm / tr if tr > 0 else 0 for mdm, tr in zip(smoothed_minus_dm, smoothed_tr)]
    dxs = [100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0 for pdi, mdi in zip(plus_dis, minus_dis)]

    if len(dxs) < period:
        return round(plus_dis[-1], 2), round(minus_dis[-1], 2), None

    adx = sum(dxs[:period]) / period
    for dx in dxs[period:]:
        adx = (adx * (period - 1) + dx) / period

    return round(plus_dis[-1], 2), round(minus_dis[-1], 2), round(adx, 2)


def classify_regime(adx):
    """Standard thresholds: ADX>=25 trending, ADX<=18 ranging, in between = transitioning/unclear."""
    if adx is None:
        return "UNKNOWN"
    if adx >= 25:
        return "TRENDING"
    if adx <= 18:
        return "RANGING"
    return "TRANSITIONING"


def calc_atr(candles: list, period: int = 14):
    """Wilder's ATR. candles: oldest-first list of {'high','low','close', ...}."""
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h, l, prev_close = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
    if len(trs) < period:
        return None
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return round(atr, 2)


def swing_high_low(candles: list, lookback: int = 20):
    """Simple rolling swing high/low over the last `lookback` candles."""
    if not candles:
        return None, None
    recent = candles[-lookback:] if len(candles) >= lookback else candles
    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]
    return (max(highs) if highs else None, min(lows) if lows else None)


def prev_day_levels(candles: list, today: dt.date):
    """Previous DAY's high/low/close -- looks at the most recent date in the
    candle list that isn't today."""
    prior = [c for c in candles if c["datetime"].date() != today]
    if not prior:
        return None
    last_date = max(c["datetime"].date() for c in prior)
    day_candles = [c for c in prior if c["datetime"].date() == last_date]
    if not day_candles:
        return None
    day_candles.sort(key=lambda c: c["datetime"])
    return {
        "pdh": max(c["high"] for c in day_candles),
        "pdl": min(c["low"] for c in day_candles),
        "pdc": day_candles[-1]["close"],
        "date": last_date.isoformat(),
    }


def opening_range(candles: list, today: dt.date, minutes: int = 15):
    """First `minutes` of today's session -- classic ORB (opening range breakout) levels."""
    today_candles = sorted([c for c in candles if c["datetime"].date() == today], key=lambda c: c["datetime"])
    if not today_candles:
        return None
    session_start = today_candles[0]["datetime"]
    cutoff = session_start + dt.timedelta(minutes=minutes)
    or_candles = [c for c in today_candles if c["datetime"] <= cutoff]
    if not or_candles:
        return None
    return {
        "orh": max(c["high"] for c in or_candles),
        "orl": min(c["low"] for c in or_candles),
        "window_minutes": minutes,
    }


def calc_vwap(candles: list, today: dt.date):
    """Volume-weighted average price for today's session. Returns None if no
    volume data is available (e.g. pure index without a tradable volume feed)."""
    today_candles = [c for c in candles if c["datetime"].date() == today]
    total_pv, total_v = 0.0, 0.0
    for c in today_candles:
        vol = c.get("volume", 0) or 0
        if vol <= 0:
            continue
        typical = (c["high"] + c["low"] + c["close"]) / 3
        total_pv += typical * vol
        total_v += vol
    if total_v == 0:
        return None
    return round(total_pv / total_v, 2)


def custom_range_levels(prev_high: float, prev_low: float, prev_close: float):
    """
    Resistance/Support (outer levels): UPDATED 2026-07-30 to plain PDH/PDL,
    based on empirical evidence across 257 trading days (NATURALGAS) --
    the Range/2 offset showed near-zero optimal bias approaching PLAIN
    PDH/PDL as the best-fit (see find_best_formula_coefficients.py,
    control-check: PDH bias +0.06pts, PDL bias -0.03pts -- effectively
    unbiased). The prior Range/2-offset formula was empirically NOT
    unbiased at divisor=2.

    Resistance Reversal / Support Reversal (inner levels): DELIBERATELY
    UNCHANGED (still Range/4) despite their own empirical bias also
    trending toward zero as the divisor grows. Applying "zero offset"
    there would make resistance_reversal == resistance (same price),
    collapsing the reversal-zone vs breakout-zone distinction the S/R
    state-machine's WATCH->ARMED->CONFIRMED logic depends on. Needs
    separate investigation (actual trade-performance impact, not just
    bias-minimization) before this one is touched.
    """
    range_ = prev_high - prev_low
    resistance = prev_high
    support = prev_low
    resistance_reversal = prev_high - range_ / 4
    support_reversal = prev_low + range_ / 4
    return {
        "pdh": round(prev_high, 2), "pdl": round(prev_low, 2), "pdc": round(prev_close, 2),
        "range": round(range_, 2),
        "resistance": round(resistance, 2), "resistance_reversal": round(resistance_reversal, 2),
        "support": round(support, 2), "support_reversal": round(support_reversal, 2),
    }


def classical_pivots(prev_high: float, prev_low: float, prev_close: float):
    """
    Standard classical pivots P/R1-R3/S1-S3, PLUS R4/S4 (added 2026-08-01 for
    sr_engine_v3.py's level ladder) using the widely-used extended-pivot
    formula: R4 = R3 + (R2-R1), S4 = S3 - (S1-S2) -- same spacing as the
    R2->R3 / S2->S3 step, projected one step further. Standard, documented
    convention (same "no proprietary math" rule as the rest of this file),
    not an invented formula.
    """
    p = (prev_high + prev_low + prev_close) / 3
    r1, s1 = 2 * p - prev_low, 2 * p - prev_high
    r2, s2 = p + (prev_high - prev_low), p - (prev_high - prev_low)
    r3, s3 = prev_high + 2 * (p - prev_low), prev_low - 2 * (prev_high - p)
    r4, s4 = r3 + (r2 - r1), s3 - (s1 - s2)
    return {"P": round(p, 2), "R1": round(r1, 2), "R2": round(r2, 2), "R3": round(r3, 2), "R4": round(r4, 2),
            "S1": round(s1, 2), "S2": round(s2, 2), "S3": round(s3, 2), "S4": round(s4, 2)}


def calc_cpr(prev_high: float, prev_low: float, prev_close: float):
    """
    Central Pivot Range: standard textbook formula (same one classical_pivots
    already uses for the plain Pivot) -- Pivot = (H+L+C)/3, BC (Bottom
    Central) = (H+L)/2, TC (Top Central) = 2*Pivot - BC. `width` (TC-BC,
    always returned as a positive number regardless of which sits above the
    other) is a standard "how wide is today's expected range" read: a narrow
    CPR is the well-known precursor to a trending/breakout day, a wide CPR
    tends to mean range-bound/choppy -- consumed by sr_engine_v3's
    regime-adaptive weighting, not decided here.
    """
    pivot = (prev_high + prev_low + prev_close) / 3
    bc = (prev_high + prev_low) / 2
    tc = 2 * pivot - bc
    return {"pivot": round(pivot, 2), "bc": round(bc, 2), "tc": round(tc, 2),
            "width": round(abs(tc - bc), 2)}


def cross_verify_wall(wall_strike: float, direction: str, market_structure: dict, tolerance_atr_mult: float = 0.5):
    """
    Cross-verifies an OI-wall (highest CE/PE OI strike, used for target
    projection) against genuine price-history structural levels of the
    MATCHING type: resistance-type levels (PDH, Swing High, Opening Range
    High, Pivot R1/R2/R3) for a CE wall; support-type levels (PDL, Swing Low,
    Opening Range Low, Pivot S1/S2/S3) for a PE wall.

    Two independently-computed S/R systems (option-writer positioning vs.
    actual price history) agreeing on the same level is a much stronger signal
    than either alone -- an OI wall with no price-history backing might just be
    where option writers happened to cluster, not a level the market actually
    respects. Returns (confirmed: bool, matched_level: dict or None).
    """
    atr = market_structure.get("atr_14") if market_structure else None
    # `atr is None`, not a truthy check -- ATR==0.0 is a real (zero-volatility)
    # reading; it still works below (max_dist=0 => no false confirmations).
    if not market_structure or atr is None:
        return False, None

    resistance_types, support_types = [], []
    pd = market_structure.get("prev_day")
    if pd:
        resistance_types.append(("PDH", pd["pdh"]))
        support_types.append(("PDL", pd["pdl"]))
    if market_structure.get("swing_high"):
        resistance_types.append(("Swing High", market_structure["swing_high"]))
    if market_structure.get("swing_low"):
        support_types.append(("Swing Low", market_structure["swing_low"]))
    orb = market_structure.get("opening_range")
    if orb:
        resistance_types.append(("Opening Range High", orb["orh"]))
        support_types.append(("Opening Range Low", orb["orl"]))
    pivots = market_structure.get("pivots")
    if pivots:
        for name, price in pivots.items():
            if name.startswith("R"):
                resistance_types.append((f"Pivot {name}", price))
            elif name.startswith("S"):
                support_types.append((f"Pivot {name}", price))

    candidates = resistance_types if direction == "CE" else support_types
    if not candidates:
        return False, None

    max_dist = atr * tolerance_atr_mult
    best = min(candidates, key=lambda c: abs(wall_strike - c[1]))
    distance = abs(wall_strike - best[1])
    if distance <= max_dist:
        return True, {"name": best[0], "price": best[1], "distance": round(distance, 2)}
    return False, None


def nearest_major_level(underlying: float, structure: dict):
    """Finds the closest REAL structural level (not an OI-wall) -- PDH/PDL, swing
    high/low, opening range, or classical pivots. Used to gate signal quality:
    a breakout/reversal near one of these means something, near a random strike
    doesn't."""
    if not structure:
        return None
    candidates = []
    pd = structure.get("prev_day")
    if pd:
        candidates.append(("PDH", pd["pdh"]))
        candidates.append(("PDL", pd["pdl"]))
        candidates.append(("PDC", pd["pdc"]))
    if structure.get("swing_high"):
        candidates.append(("Swing High", structure["swing_high"]))
    if structure.get("swing_low"):
        candidates.append(("Swing Low", structure["swing_low"]))
    orb = structure.get("opening_range")
    if orb:
        candidates.append(("Opening Range High", orb["orh"]))
        candidates.append(("Opening Range Low", orb["orl"]))
    pivots = structure.get("pivots")
    if pivots:
        for name, price in pivots.items():
            candidates.append((f"Pivot {name}", price))
    if not candidates:
        return None
    name, price = min(candidates, key=lambda c: abs(underlying - c[1]))
    return {"name": name, "price": price, "distance": round(abs(underlying - price), 2)}


def detect_mother_candle(candles, atr, volume_sma_period=20):
    """
    Detects the most recent 'Mother Candle' (a large-range, strong-body,
    volume-backed candle marking a consolidation anchor), counts subsequent
    'Inside Bars' (candles staying within its range), and reports whether a
    genuine CLOSE-confirmed breakout has occurred (wick-only pokes outside
    the range don't count -- the close must be outside).

    candles: oldest-first list of {'open','high','low','close','volume',...}.
    This is a real, well-defined price-action pattern, genuinely distinct
    from our existing OI-based evidence -- complements rather than
    duplicates it.
    """
    if not candles or len(candles) < volume_sma_period + 2 or not atr:
        return {"found": False}

    volumes = [c.get("volume", 0) for c in candles]

    mother_idx = None
    for i in range(len(candles) - 2, volume_sma_period - 1, -1):
        c = candles[i]
        rng = c["high"] - c["low"]
        if rng <= 0:
            continue
        body = abs(c["close"] - c["open"])
        body_ratio = body / rng
        is_doji = body_ratio < 0.1
        vol_sma20 = sum(volumes[max(0, i - volume_sma_period):i]) / volume_sma_period
        if rng > atr * 0.8 and body_ratio > 0.60 and not is_doji and vol_sma20 and c.get("volume", 0) > vol_sma20:
            mother_idx = i
            break

    if mother_idx is None:
        return {"found": False}

    mother = candles[mother_idx]
    mother_high, mother_low = mother["high"], mother["low"]

    inside_bar_count = 0
    breakout_direction, breakout_confirmed = None, False
    for c in candles[mother_idx + 1:]:
        if c["high"] <= mother_high and c["low"] >= mother_low:
            inside_bar_count += 1
            continue
        if c["close"] > mother_high:
            breakout_direction, breakout_confirmed = "bullish", True
        elif c["close"] < mother_low:
            breakout_direction, breakout_confirmed = "bearish", True
        break   # first candle to leave the range ends the inside-bar count either way

    return {
        "found": True, "mother_high": mother_high, "mother_low": mother_low,
        "mother_range": round(mother_high - mother_low, 2), "inside_bar_count": inside_bar_count,
        "breakout_direction": breakout_direction, "breakout_confirmed": breakout_confirmed,
    }


def detect_liquidity_sweep(candles, pdh, pdl, lookback=5):
    """
    Detects a Liquidity Sweep + Reclaim: price briefly wicks through PDH/PDL
    (a stop-hunt/shakeout) then the current close reclaims back inside the
    prior range -- often precedes a genuine move in the opposite direction of
    the sweep. Distinct from Mother Candle (which is about a consolidation
    range, not the prior day's H/L specifically).

    Bullish: swept below PDL, then closed back above it.
    Bearish: swept above PDH, then closed back below it.
    """
    if not candles or len(candles) < 2 or not pdh or not pdl:
        return {"swept": None, "reclaimed": False}

    recent = candles[-lookback:]
    latest = recent[-1]

    bullish = None
    swept_lows = [(i, c["low"]) for i, c in enumerate(recent) if c["low"] < pdl]
    if swept_lows and latest["close"] > pdl:
        last_idx = max(i for i, _ in swept_lows)
        bullish = {"swept": "bullish", "reclaimed": True,
                   "sweep_extreme": min(v for _, v in swept_lows), "_idx": last_idx}

    bearish = None
    swept_highs = [(i, c["high"]) for i, c in enumerate(recent) if c["high"] > pdh]
    if swept_highs and latest["close"] < pdh:
        last_idx = max(i for i, _ in swept_highs)
        bearish = {"swept": "bearish", "reclaimed": True,
                   "sweep_extreme": max(v for _, v in swept_highs), "_idx": last_idx}

    # A choppy/whipsaw day can sweep+reclaim BOTH PDL and PDH inside the same
    # lookback window -- returning on the first match (bullish, checked
    # first) silently dropped a genuine bearish sweep whenever both occurred.
    # Report whichever sweep happened more recently within the window.
    result = None
    if bullish and bearish:
        result = bullish if bullish["_idx"] >= bearish["_idx"] else bearish
    else:
        result = bullish or bearish
    if result:
        result = {k: v for k, v in result.items() if k != "_idx"}
        return result
    return {"swept": None, "reclaimed": False}


def build_market_structure(candles: list, now: dt.datetime = None):
    """
    One-shot: compute everything from a candle list. Returns None fields
    gracefully if there isn't enough data yet (e.g. first day of collection --
    PDH/PDL need at least one prior trading day of candles, which won't exist
    until this has run for more than a day).
    """
    now = now or dt.datetime.now()
    today = now.date()

    # NON-REPAINT SAFETY: drop the last candle if it hasn't genuinely closed
    # yet. Infer the candle interval from the spacing between the last two
    # candles (robust to whatever interval is actually being used) rather
    # than hardcoding it -- a still-forming candle's high/low/close can
    # still change, and including it would make every level/ATR/Mother-
    # Candle value computed from it silently "repaint" once it closes.
    if len(candles) >= 2:
        try:
            last_dt = dt.datetime.fromisoformat(candles[-1]["datetime"]) if isinstance(candles[-1]["datetime"], str) else candles[-1]["datetime"]
            prev_dt = dt.datetime.fromisoformat(candles[-2]["datetime"]) if isinstance(candles[-2]["datetime"], str) else candles[-2]["datetime"]
            interval = last_dt - prev_dt
            if interval.total_seconds() > 0 and (last_dt + interval) > now.replace(tzinfo=last_dt.tzinfo):
                candles = candles[:-1]   # last candle is still forming -- exclude it
        except (KeyError, ValueError, TypeError):
            pass   # if timestamps are missing/malformed, fall back to using all candles as-is

    if not candles:
        return {"atr_14": None, "swing_high": None, "swing_low": None, "prev_day": None,
                "opening_range": None, "vwap": None, "pivots": None, "custom_levels": None, "candle_count": 0,
                "plus_di": None, "minus_di": None, "adx": None, "regime": "UNKNOWN", "mother_candle": {"found": False},
                "liquidity_sweep": {"swept": None, "reclaimed": False}, "recent_candles": [], "cpr": None}

    atr = calc_atr(candles, period=14)
    swing_h, swing_l = swing_high_low(candles, lookback=20)
    pdl = prev_day_levels(candles, today)
    orb = opening_range(candles, today, minutes=15)
    vwap = calc_vwap(candles, today)
    pivots = classical_pivots(pdl["pdh"], pdl["pdl"], pdl["pdc"]) if pdl else None
    custom_levels = custom_range_levels(pdl["pdh"], pdl["pdl"], pdl["pdc"]) if pdl else None
    cpr = calc_cpr(pdl["pdh"], pdl["pdl"], pdl["pdc"]) if pdl else None
    plus_di, minus_di, adx = calc_adx(candles, period=14)
    regime = classify_regime(adx)
    mother_candle = detect_mother_candle(candles, atr)
    liquidity_sweep = detect_liquidity_sweep(candles, pdl["pdh"] if pdl else None, pdl["pdl"] if pdl else None)

    return {
        "atr_14": atr, "swing_high": swing_h, "swing_low": swing_l,
        "prev_day": pdl, "opening_range": orb, "vwap": vwap, "pivots": pivots,
        "custom_levels": custom_levels, "cpr": cpr,
        "candle_count": len(candles),
        "plus_di": plus_di, "minus_di": minus_di, "adx": adx, "regime": regime,
        "mother_candle": mother_candle, "liquidity_sweep": liquidity_sweep,
        "recent_candles": candles[-30:],   # non-repaint-filtered (forming candle excluded) -- for 3-candle-close signal confirmation
    }
