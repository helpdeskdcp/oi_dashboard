"""
institutional_levels.py -- Milestone 20, Phase 1: Weighted Composite
Support/Resistance + Role-Reversal detection.

Deliberately NOT a new engine -- this composes market_structure.py's
(PDH/PDL/PDC, classical pivots, CPR, swing high/low, VWAP) and
oi_engine.py's (oi_walls) EXISTING, already-tested primitives into one
ranked level list, and adds two genuinely new pieces on top: role-
reversal pattern detection (a level flipping support<->resistance) and
a lightweight live-direction classifier. No parallel signal-generation
math, no parallel paper-trading engine -- ai_trading_engine.py/
oi_engine.generate_signal() remain the only place a BUY CE/PE
recommendation is actually decided.

Every function here is pure: given candles/rows/levels, it returns a
result -- never fetches its own data, never writes to a database, never
sends a Telegram message. That's deliberate, matching market_structure.py/
sr_probability_engine.py's own convention, and keeps this module trivially
unit-testable without a live DB or broker session.

Scope for this milestone (explicit, per the request that authorized this
file): weighted_levels(), detect_role_reversal(), instrument-aware
thresholds, and classify_market_state() only. NOT in scope yet: adaptive/
Bayesian parameter learning, a new backtest framework, or autonomous
order execution -- those are separate, later milestones.

`candles` throughout this module: a plain list of dict-like records
(oldest-first), each with "datetime"/"open"/"high"/"low"/"close"/
"volume" keys -- the exact shape market_structure.py's own functions
already require (see build_market_structure()'s own docstring). A
caller holding a pandas DataFrame (e.g. from
agents.trading_intelligence.data_access.load_candles()) converts via
.to_dict("records") before calling in here -- this module takes no
pandas dependency of its own.
"""
import datetime as dt

import market_structure
from oi_engine import oi_walls

# Instrument-aware thresholds -- deliberately NOT one shared
# breakout/retest formula for every symbol (a NATURALGAS 0.20 buffer and
# a NIFTY 20-point buffer are not the same "how far is a real breakout"
# question). Mini/micro contracts (NATGASMINI/CRUDEOILM/GOLDM/SILVERM)
# reuse their base commodity's numbers -- same underlying instrument,
# just a smaller lot, so the same structural distances apply.
PROFILE_THRESHOLDS = {
    "NIFTY":       {"breakout_buffer": 20,   "retest_tolerance": 5,    "sl_buffer": 15,   "confidence_threshold": 70},
    "BANKNIFTY":   {"breakout_buffer": 50,   "retest_tolerance": 15,   "sl_buffer": 25,   "confidence_threshold": 75},
    "SENSEX":      {"breakout_buffer": 80,   "retest_tolerance": 20,   "sl_buffer": 40,   "confidence_threshold": 72},
    "NATURALGAS":  {"breakout_buffer": 0.20, "retest_tolerance": 0.05, "sl_buffer": 0.12, "confidence_threshold": 80},
    "NATGASMINI":  {"breakout_buffer": 0.20, "retest_tolerance": 0.05, "sl_buffer": 0.12, "confidence_threshold": 80},
    "CRUDEOIL":    {"breakout_buffer": 0.8,  "retest_tolerance": 0.2,  "sl_buffer": 0.5,  "confidence_threshold": 78},
    "CRUDEOILM":   {"breakout_buffer": 0.8,  "retest_tolerance": 0.2,  "sl_buffer": 0.5,  "confidence_threshold": 78},
    "GOLD":        {"breakout_buffer": 30,   "retest_tolerance": 10,  "sl_buffer": 20,   "confidence_threshold": 74},
    "GOLDM":       {"breakout_buffer": 30,   "retest_tolerance": 10,  "sl_buffer": 20,   "confidence_threshold": 74},
    "SILVER":      {"breakout_buffer": 60,   "retest_tolerance": 20,  "sl_buffer": 40,   "confidence_threshold": 76},
    "SILVERM":     {"breakout_buffer": 60,   "retest_tolerance": 20,  "sl_buffer": 40,   "confidence_threshold": 76},
}
_DEFAULT_PROFILE = {"breakout_buffer": 0, "retest_tolerance": 0, "sl_buffer": 0, "confidence_threshold": 70}


def get_profile(symbol: str) -> dict:
    """Never raises for an unmapped symbol -- a zero-buffer default
    (never fabricating a plausible-looking number for an instrument
    nobody has actually calibrated yet)."""
    return PROFILE_THRESHOLDS.get(symbol, _DEFAULT_PROFILE)


# Section 3's own weight formula -- coefficients sum to 1.0 by
# construction (checked below, not just by eye, same discipline
# strike_intelligence.py's own _SCORE_WEIGHTS already holds to).
_LEVEL_WEIGHTS = {
    "pivot": 0.15, "swing": 0.20, "oi": 0.25, "vwap": 0.15,
    "round_number": 0.10, "volume_profile": 0.15,
}
assert abs(sum(_LEVEL_WEIGHTS.values()) - 1.0) < 1e-9, "_LEVEL_WEIGHTS must sum to 1.0"
MAJOR_LEVEL_MIN_WEIGHT = 0.65


def _candle_wicks(candle: dict) -> tuple:
    """(body, upper_wick, lower_wick) -- plain OHLC arithmetic, same
    definitions candlestick_patterns.py's own _candle_metrics() uses,
    re-derived inline here rather than importing a private helper
    across modules."""
    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
    body = abs(c - o)
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    return body, upper_wick, lower_wick


def _round_number_candidates(underlying: float, step: float, *, span: int = 3) -> list:
    """Nearest round numbers around `underlying`, spaced by `step` (the
    option chain's own strike step -- already how this codebase defines
    "a round number" for a given instrument, e.g. 50 for NIFTY, 100 for
    BANKNIFTY, 10 for NATURALGAS -- never a second, independent
    definition of "round")."""
    if not step or step <= 0:
        return []
    base = round(underlying / step) * step
    return [round(base + i * step, 2) for i in range(-span, span + 1)]


def _volume_reaction_levels(candles: list, *, lookback: int = 30) -> list:
    """A candle whose volume meaningfully exceeds its own recent average
    marks its high/low as a level real participation reacted to at --
    the "Volume Profile Peaks" source, computed from the same candle
    series every other source here already reads, not a separate volume-
    profile histogram build."""
    recent = candles[-lookback:] if len(candles) > lookback else candles
    vols = [c.get("volume", 0) or 0 for c in recent]
    if len(vols) < 3 or sum(vols) == 0:
        return []
    avg_vol = sum(vols) / len(vols)
    levels = []
    for c in recent:
        vol = c.get("volume", 0) or 0
        if avg_vol > 0 and vol >= avg_vol * 1.8:
            levels.append(c["high"])
            levels.append(c["low"])
    return levels


def weighted_levels(symbol: str, *, candles: list, rows: list, atm: float, underlying: float,
                     today: dt.date | None = None, strike_step: float | None = None) -> list:
    """The composite ranked level list Section 3 asks for. Clusters raw
    candidates from every source within a tolerance window, scores each
    cluster by which CATEGORIES (not how many individual candidates --
    two pivot-derived levels landing in the same cluster still only
    count "pivot" once) are represented using _LEVEL_WEIGHTS, and keeps
    only clusters at/above MAJOR_LEVEL_MIN_WEIGHT.

    Returns a list of {"level", "type", "weight", "sources"}, sorted by
    weight (desc), matching the spec's own example shape exactly."""
    today = today or dt.date.today()
    profile = get_profile(symbol)
    # Clustering tolerance: wider than the tight retest_tolerance (which
    # is about confirming a SPECIFIC already-known level, not deciding
    # whether two raw candidates are "the same level" in the first
    # place) -- documented heuristic, not claimed as optimal.
    tolerance = max(profile["retest_tolerance"] * 3, profile["breakout_buffer"] * 0.5, 0.01)

    candidates = []  # list of (price, category, source_label)

    prev_day = market_structure.prev_day_levels(candles, today)
    if prev_day:
        candidates.append((prev_day["pdh"], "pivot", "PDH"))
        candidates.append((prev_day["pdl"], "pivot", "PDL"))
        candidates.append((prev_day["pdc"], "pivot", "PDC"))
        pivots = market_structure.classical_pivots(prev_day["pdh"], prev_day["pdl"], prev_day["pdc"])
        for name, price in pivots.items():
            candidates.append((price, "pivot", f"PIVOT_{name}"))
        cpr = market_structure.calc_cpr(prev_day["pdh"], prev_day["pdl"], prev_day["pdc"])
        candidates.append((cpr["tc"], "pivot", "CPR_TC"))
        candidates.append((cpr["bc"], "pivot", "CPR_BC"))
        candidates.append((cpr["pivot"], "pivot", "CPR_PIVOT"))

    swing_high, swing_low = market_structure.swing_high_low(candles)
    if swing_high is not None:
        candidates.append((swing_high, "swing", "SWING_HIGH"))
    if swing_low is not None:
        candidates.append((swing_low, "swing", "SWING_LOW"))

    if rows:
        support_walls, resistance_walls = oi_walls(rows)
        for w in support_walls:
            candidates.append((w.strike, "oi", "OI_WALL_PE"))
        for w in resistance_walls:
            candidates.append((w.strike, "oi", "OI_WALL_CE"))

    vwap = market_structure.calc_vwap(candles, today)
    if vwap is not None:
        candidates.append((vwap, "vwap", "VWAP"))

    step = strike_step
    if step is None and rows and len(rows) >= 2:
        strikes_sorted = sorted({r.strike for r in rows})
        step = strikes_sorted[1] - strikes_sorted[0] if len(strikes_sorted) >= 2 else None
    for price in _round_number_candidates(underlying, step or 0):
        candidates.append((price, "round_number", "ROUND_NUMBER"))

    for price in _volume_reaction_levels(candles):
        candidates.append((price, "volume_profile", "VOLUME_REACTION"))

    candidates = [c for c in candidates if c[0] is not None]
    if not candidates:
        return []
    candidates.sort(key=lambda c: c[0])

    clusters = []
    current = [candidates[0]]
    for cand in candidates[1:]:
        if abs(cand[0] - current[-1][0]) <= tolerance:
            current.append(cand)
        else:
            clusters.append(current)
            current = [cand]
    clusters.append(current)

    results = []
    for cluster in clusters:
        categories_present = {c[1] for c in cluster}
        weight = round(sum(_LEVEL_WEIGHTS[cat] for cat in categories_present), 4)
        if weight < MAJOR_LEVEL_MIN_WEIGHT:
            continue
        level_price = round(sum(c[0] for c in cluster) / len(cluster), 2)
        level_type = "RESISTANCE" if level_price >= underlying else "SUPPORT"
        sources = sorted({c[2] for c in cluster})
        results.append({"level": level_price, "type": level_type, "weight": weight, "sources": sources})

    results.sort(key=lambda r: r["weight"], reverse=True)
    return results


def _find_retest_from_above(candles: list, *, start_idx: int, level: float, tolerance: float, lookahead: int):
    """Resistance-that-broke -> defended-as-support retest: a later
    candle dips back down to within `tolerance` of `level` (from above)
    and closes back above it. Returns (index, candle) so callers never
    need a value-equality re-lookup (candles.index(...)), which would be
    fragile if two candles happen to share identical OHLCV values."""
    for idx in range(start_idx, min(start_idx + lookahead, len(candles))):
        c = candles[idx]
        if c["low"] <= level + tolerance and c["close"] > level:
            return idx, c
    return None, None


def _find_retest_from_below(candles: list, *, start_idx: int, level: float, tolerance: float, lookahead: int):
    """Support-that-broke -> defended-as-resistance retest: a later
    candle pokes back up to within `tolerance` of `level` (from below)
    and closes back below it. Same (index, candle) return shape as
    _find_retest_from_above()."""
    for idx in range(start_idx, min(start_idx + lookahead, len(candles))):
        c = candles[idx]
        if c["high"] >= level - tolerance and c["close"] < level:
            return idx, c
    return None, None


def _reversal_confidence(defending_wick: float, body: float, upper_bound: float, lower_bound: float) -> int:
    """Transparent arithmetic, not a model (same honesty rule every
    other confidence number in this codebase already follows) -- base
    60, plus up to 25 more for how dominant the defending wick is over
    the candle's body (a 2x wick barely clears the pattern's own
    requirement; a 5x+ wick is a much more decisive rejection), plus up
    to 15 more for how cleanly the close cleared the level relative to
    the body size. Clamped to [60, 98] -- never claims certainty."""
    wick_ratio = (defending_wick / body) if body > 0 else 5.0
    wick_bonus = min(25, round((wick_ratio - 2.0) * 8))
    clearance = abs(upper_bound - lower_bound)
    clearance_bonus = min(15, round((clearance / max(body, 0.01)) * 5))
    return max(60, min(98, 60 + max(0, wick_bonus) + max(0, clearance_bonus)))


def detect_role_reversal(level: float, candles: list, *, profile: dict | None = None, lookahead: int = 8) -> dict | None:
    """Scans `candles` (oldest-first) for the MOST RECENT completed
    breakout/breakdown -> defended-retest pattern at `level`, in either
    direction. Returns None if no complete pattern exists in this
    window -- this is intentionally stateless (re-derives the pattern
    from candle history every call), so a caller running this every new
    candle never needs its own persisted "current role" to stay correct;
    the candle history itself IS the state.

    `profile`: a PROFILE_THRESHOLDS-shaped dict (breakout_buffer/
    retest_tolerance) -- pass get_profile(symbol) for real instrument-
    aware behavior; defaults to zero buffers (any close through the
    level counts as a breakout) if omitted, matching every other
    profile-aware function in this module."""
    thresholds = profile or _DEFAULT_PROFILE
    buffer = thresholds["breakout_buffer"]
    tolerance = thresholds["retest_tolerance"]

    best = None  # (retest_candle_index, result_dict)
    for i in range(len(candles) - 1):
        c = candles[i]
        if c["close"] > level + buffer:
            idx, retest = _find_retest_from_above(candles, start_idx=i + 1, level=level, tolerance=tolerance, lookahead=lookahead)
            if retest is not None:
                body, _upper, lower = _candle_wicks(retest)
                if lower >= body * 2:
                    result = {"level": level, "previous_role": "RESISTANCE", "current_role": "SUPPORT",
                              "confidence": _reversal_confidence(lower, body, retest["close"], level)}
                    if best is None or idx > best[0]:
                        best = (idx, result)
        if c["close"] < level - buffer:
            idx, retest = _find_retest_from_below(candles, start_idx=i + 1, level=level, tolerance=tolerance, lookahead=lookahead)
            if retest is not None:
                body, upper, _lower = _candle_wicks(retest)
                if upper >= body * 2:
                    result = {"level": level, "previous_role": "SUPPORT", "current_role": "RESISTANCE",
                              "confidence": _reversal_confidence(upper, body, level, retest["close"])}
                    if best is None or idx > best[0]:
                        best = (idx, result)
    return best[1] if best else None


# Live direction states (Section 6).
TRENDING_UP = "TRENDING_UP"
TRENDING_DOWN = "TRENDING_DOWN"
RANGE = "RANGE"
BREAKOUT_WATCH = "BREAKOUT_WATCH"
BREAKDOWN_WATCH = "BREAKDOWN_WATCH"
BULLISH_RETEST_ACTIVE = "BULLISH_RETEST_ACTIVE"
BEARISH_RETEST_ACTIVE = "BEARISH_RETEST_ACTIVE"
REVERSAL_RISK = "REVERSAL_RISK"


def _ema(values: list, period: int) -> float | None:
    """Standard EMA, plain Python (no pandas dependency in this module
    -- see module docstring). None if there isn't enough history yet,
    never a value computed from a too-short series pretending to be a
    real EMA."""
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def classify_market_state(symbol: str, *, candles: list, levels: list, underlying: float,
                           vwap: float | None = None, oi_lean: float = 0.0) -> dict:
    """Section 6's live direction classifier. Every component of
    direction_score is a REAL, already-available read (EMA20/50
    alignment, VWAP side, a caller-supplied OI lean -2..+2 from
    oi_engine.net_oi_buildup_lean, recent price structure, momentum,
    and whether any `levels` entry has an active role-reversal right
    now) -- never a fabricated input. Returns {"state", "score",
    "components"} so the score itself stays inspectable, not just the
    final label."""
    closes = [c["close"] for c in candles]
    ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
    if ema20 is None or ema50 is None or ema20 == ema50:
        ema_alignment = 0.0
    else:
        ema_alignment = 1.0 if ema20 > ema50 else -1.0

    if vwap is None or underlying == vwap:
        vwap_alignment = 0.0
    else:
        vwap_alignment = 1.0 if underlying > vwap else -1.0

    oi_direction = max(-1.0, min(1.0, oi_lean / 2.0))  # oi_lean is -2..+2 -- normalize to -1..1

    recent = closes[-6:] if len(closes) >= 6 else closes
    price_structure = 0.0
    if len(recent) >= 2:
        price_structure = 1.0 if recent[-1] > recent[0] else (-1.0 if recent[-1] < recent[0] else 0.0)

    momentum_score = 0.0
    if len(closes) >= 6 and closes[-6]:
        momentum_score = max(-1.0, min(1.0, (closes[-1] - closes[-6]) / closes[-6] * 20))

    active_reversal = None
    for lvl in levels:
        result = detect_role_reversal(lvl["level"], candles, profile=get_profile(symbol))
        if result:
            active_reversal = result
            break
    retest_score = 0.0
    if active_reversal:
        retest_score = 1.0 if active_reversal["current_role"] == "SUPPORT" else -1.0

    direction_score = round(
        ema_alignment * 0.20 + vwap_alignment * 0.15 + oi_direction * 0.25 +
        price_structure * 0.20 + momentum_score * 0.10 + retest_score * 0.10,
        3,
    )

    if active_reversal and active_reversal["current_role"] == "SUPPORT":
        state = BULLISH_RETEST_ACTIVE
    elif active_reversal and active_reversal["current_role"] == "RESISTANCE":
        state = BEARISH_RETEST_ACTIVE
    elif direction_score >= 0.5:
        state = TRENDING_UP
    elif direction_score <= -0.5:
        state = TRENDING_DOWN
    elif 0.15 <= direction_score < 0.5:
        state = BREAKOUT_WATCH
    elif -0.5 < direction_score <= -0.15:
        state = BREAKDOWN_WATCH
    elif ema_alignment != 0 and vwap_alignment != 0 and ema_alignment != vwap_alignment:
        state = REVERSAL_RISK  # trend and VWAP-side disagree -- genuine conflicting evidence
    else:
        state = RANGE

    return {
        "state": state, "score": direction_score,
        "components": {
            "ema_alignment": ema_alignment, "vwap_alignment": vwap_alignment, "oi_direction": oi_direction,
            "price_structure": price_structure, "momentum_score": round(momentum_score, 3), "retest_score": retest_score,
        },
    }
