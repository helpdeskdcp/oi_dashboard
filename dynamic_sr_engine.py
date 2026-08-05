#!/usr/bin/env python3
"""
dynamic_sr_engine.py -- Dynamic Support & Resistance Engine (V1).

Computes a PDH/PDL-based resistance/support ladder that keeps extending
without limit as price moves beyond it, and turns clean breakouts/
breakdowns (candle-close + volume + trend confirmed, false-breakout
filtered) into BUY/SELL signals with stop-loss, staged targets, trailing-
stop-loss, and a 0-100 confidence score.

Pure price/volume engine -- no OI dependency by design (this is V1 of the
spec; VWAP/OI-walls/PCR/Greeks/ATR/CPR/market-structure are an explicitly
separate V2 upgrade, not part of this module).

Candle convention: oldest-first list of
{'datetime', 'open', 'high', 'low', 'close', 'volume'}.
"""

import datetime as dt


# ---------------------------------------------------------------------------
# Level ladder
# ---------------------------------------------------------------------------

def build_levels(pdh: float, pdl: float) -> dict:
    """R1-R3 / S1-S3 exactly per spec.

    NOTE the ladder is NOT value-sorted: R3 (PDH-Range2) sits BELOW R1
    (PDH), which sits below R2 (PDH+Range1) -- R3 is the inner
    'reversal/breakout-watch' zone, R2 is the outer first target. Dynamic
    R4+ extends from R3 (not R2) -- and S4+ extends from S3 (not S2) --
    exactly as the spec's worked examples show (R4=R3+Range1, S4=S3-Range1).
    """
    range_ = pdh - pdl
    range1 = range_ / 2
    range2 = range_ / 4
    return {
        "pdh": pdh, "pdl": pdl, "range": range_, "range1": range1, "range2": range2,
        "resistances": [pdh, pdh + range1, pdh - range2],   # R1, R2, R3
        "supports": [pdl, pdl - range1, pdl + range2],       # S1, S2, S3
    }


def extend_levels(levels: dict, current_price: float) -> dict:
    """Appends R4, R5, ... / S4, S5, ... (step = Range1) so the ladder always
    has a resistance at/above and a support at/below current price. No
    predefined limit, per spec. No-op if range1 is 0 (PDH==PDL) to avoid an
    infinite loop."""
    range1 = levels["range1"]
    resistances = list(levels["resistances"])
    supports = list(levels["supports"])
    if range1 > 0:
        while current_price > resistances[-1]:
            resistances.append(resistances[-1] + range1)
        while current_price < supports[-1]:
            supports.append(supports[-1] - range1)
    out = dict(levels)
    out["resistances"] = resistances
    out["supports"] = supports
    return out


# ---------------------------------------------------------------------------
# Candle / volume / trend helpers
# ---------------------------------------------------------------------------

def _candle_metrics(c: dict) -> dict:
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    rng = h - l
    body = abs(cl - o)
    return {"range": rng, "body": body, "body_ratio": (body / rng) if rng > 0 else 0.0,
            "is_bullish": cl > o}


def avg_volume(candles: list, period: int = 20):
    """20-period average volume -- the spec's AvgVolume input."""
    if len(candles) < period:
        return None
    vols = [c.get("volume", 0) or 0 for c in candles[-period:]]
    return sum(vols) / period


def detect_trend(candles: list, fast: int = 9, slow: int = 21) -> str:
    """Self-contained EMA(9)/EMA(21) cross+slope trend read -- this engine
    has no market_structure/ADX dependency by design. Returns 'bullish',
    'bearish', or 'sideways'."""
    closes = [c["close"] for c in candles]
    if len(closes) < slow + 2:
        return "sideways"

    def ema_series(values, period):
        k = 2 / (period + 1)
        series = [values[0]]
        for v in values[1:]:
            series.append(v * k + series[-1] * (1 - k))
        return series

    ema_fast = ema_series(closes, fast)
    ema_slow = ema_series(closes, slow)
    fast_now, fast_prev, slow_now = ema_fast[-1], ema_fast[-2], ema_slow[-1]
    if fast_now > slow_now and fast_now > fast_prev:
        return "bullish"
    if fast_now < slow_now and fast_now < fast_prev:
        return "bearish"
    return "sideways"


def momentum_roc(candles: list, period: int = 5) -> float:
    """Rate of change over the last `period` candles, used as the
    confidence score's 10% momentum component."""
    if len(candles) < period + 1:
        return 0.0
    now, prior = candles[-1]["close"], candles[-1 - period]["close"]
    if prior == 0:
        return 0.0
    return (now - prior) / prior


def _is_false_breakout(candle: dict, level: float, direction: str) -> bool:
    """A genuine breakout candle closes with real conviction: body >= 30% of
    its own range. Filters dojis / wick-dominated bars where the close
    poked past the level but the candle itself shows no real commitment."""
    return _candle_metrics(candle)["body_ratio"] < 0.30


# ---------------------------------------------------------------------------
# Confidence score
# ---------------------------------------------------------------------------

def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def compute_confidence(candle, level, range1, volume, avg_vol, trend, direction, roc) -> float:
    """0-100, per spec weights: 40% breakout distance, 25% volume, 15%
    candle body conviction, 10% trend agreement, 10% momentum."""
    m = _candle_metrics(candle)
    close = candle["close"]

    breakout_dist = (close - level) if direction == "bullish" else (level - close)
    breakout_score = _clamp01(breakout_dist / range1) * 40 if range1 else 0.0

    # No real volume feed (pure index -- see evaluate()'s has_volume_feed) ->
    # this factor is inapplicable, not failed: award full marks rather than
    # zero, same "not applicable != violated" logic as the signal gate.
    if avg_vol:
        vol_ratio = volume / avg_vol
        volume_score = _clamp01(vol_ratio / 2.0) * 25   # 2x avg volume = full marks
    else:
        volume_score = 25.0

    candle_score = _clamp01(m["body_ratio"]) * 15

    trend_ok = (trend == "bullish") if direction == "bullish" else (trend == "bearish")
    trend_score = 10.0 if trend_ok else 0.0

    momentum_score = (_clamp01(roc) if direction == "bullish" else _clamp01(-roc)) * 10

    return round(breakout_score + volume_score + candle_score + trend_score + momentum_score, 1)


MIN_TRADEABLE_CONFIDENCE = 60.0   # spec's rating table: below this is "No Trade", not a real signal

# Backtest of 322 trades (NIFTY/BANKNIFTY/SENSEX/FINNIFTY, 2025-07-24 to
# 2026-07-31) found the 10:00-10:59 hour alone was net -659 points over 33
# trades (17 SL, 0 target hits) -- a post-opening-range fakeout window across
# all 4 symbols -> ENTRY_BLACKOUT_START/END skips signal generation there.
# (Also tried: requiring the breakout to hold for a 2nd candle before entry.
# Rejected -- it didn't reduce the stop-loss rate, since most reversals hit
# 1-2 candles AFTER entry regardless of when entry happens, and delaying
# entry by a candle just ate into the 30-min hold window's favorable drift,
# which is where nearly all of this engine's realized profit actually comes
# from -- see run_dynamic_sr_backtest's TARGET HIT rate, ~2% of trades.)
ENTRY_BLACKOUT_START = dt.time(10, 0)
ENTRY_BLACKOUT_END = dt.time(11, 0)   # exclusive


def _in_entry_blackout(candle_time) -> bool:
    if candle_time is None:
        return False
    t = candle_time.time() if hasattr(candle_time, "time") else candle_time
    return ENTRY_BLACKOUT_START <= t < ENTRY_BLACKOUT_END


def rate_signal(confidence: float) -> str:
    if confidence >= 90:
        return "Strong"
    if confidence >= 75:
        return "Tradeable"
    if confidence >= 60:
        return "Weak"
    return "No Trade"


# ---------------------------------------------------------------------------
# Signal + targets + trailing SL
# ---------------------------------------------------------------------------

def _build_signal(direction, entry, sl, range1, confidence, level_idx, level_price):
    sign = 1 if direction == "BUY" else -1
    targets = [round(entry + sign * n * range1, 2) for n in (1, 2, 3)]
    return {
        "direction": direction, "entry": round(entry, 2), "stop_loss": round(sl, 2),
        "targets": targets, "confidence": confidence, "rating": rate_signal(confidence),
        "broken_level_index": level_idx + 1, "broken_level_price": round(level_price, 2),
        "range1": round(range1, 2),
    }


def update_trailing_sl(direction: str, entry: float, sl: float, targets: list, current_price: float) -> float:
    """After Tn is hit, SL moves to T(n-1) (T0 == entry). Stateless: pass the
    signal's fixed entry/targets and current SL, get back the SL that should
    apply now. Caller persists the returned value per open position."""
    checkpoints = [entry] + list(targets)
    new_sl = sl
    for i, target in enumerate(targets):
        hit = (current_price >= target) if direction == "BUY" else (current_price <= target)
        if hit:
            new_sl = checkpoints[i]   # move SL to the PREVIOUS checkpoint
    return new_sl


# ---------------------------------------------------------------------------
# One-shot evaluation
# ---------------------------------------------------------------------------

def evaluate(candles: list, pdh: float, pdl: float, current_price: float = None,
             min_tradeable_confidence: float = None) -> dict:
    """Builds the ladder, extends it to current price, checks the latest
    CLOSED candle for a fresh breakout/breakdown, and returns the full
    dashboard payload (levels + signal + SL/targets/confidence/trend).

    min_tradeable_confidence: overrides MIN_TRADEABLE_CONFIDENCE when given
    (backtest-only tuning, see app.py's ENGINE_PARAM_SPECS["dynamic-sr"]) --
    None (the default) preserves today's exact behavior, so the live call
    site (app.py, display-only) is untouched and stays byte-identical."""
    if not candles:
        return None
    min_tradeable_confidence = (MIN_TRADEABLE_CONFIDENCE if min_tradeable_confidence is None
                                 else min_tradeable_confidence)
    current_price = current_price if current_price is not None else candles[-1]["close"]

    levels = extend_levels(build_levels(pdh, pdl), current_price)
    resistances, supports, range1 = levels["resistances"], levels["supports"], levels["range1"]

    latest = candles[-1]
    prev_close = candles[-2]["close"] if len(candles) >= 2 else None
    avg_vol = avg_volume(candles)
    volume = latest.get("volume", 0) or 0
    # Pure indices (NIFTY, BANKNIFTY, SENSEX...) have no tradable-volume feed
    # -- every candle reports volume=0, same data gap market_structure.py's
    # calc_vwap already documents. Gating every signal on "volume > avg"
    # would then silently block ALL signals forever for those symbols, so
    # the check only applies when a genuine (non-zero) volume feed exists.
    has_volume_feed = avg_vol is not None and avg_vol > 0
    volume_confirmed = (volume > avg_vol) if has_volume_feed else True
    trend = detect_trend(candles)
    roc = momentum_roc(candles)

    signal = None
    blackout = _in_entry_blackout(latest.get("datetime"))

    if not blackout:
        broken_r_idx = None
        for i, level in enumerate(resistances):
            if latest["close"] > level and (prev_close is None or prev_close <= level):
                broken_r_idx = i   # keep the highest-index level broken this candle
        if broken_r_idx is not None:
            level = resistances[broken_r_idx]
            if (volume_confirmed and trend == "bullish"
                    and not _is_false_breakout(latest, level, "bullish")):
                entry = latest["close"]
                # SL = nearest ladder rung strictly below entry, by PRICE (not by
                # index -- R2 sits ABOVE R3 in price despite the lower index, so
                # an index-based "previous level" can land SL on the wrong side
                # of entry; see project notes).
                below = [r for r in resistances if r < entry]
                sl = max(below) if below else levels["pdl"]
                confidence = compute_confidence(latest, level, range1, volume, avg_vol, trend, "bullish", roc)
                if confidence >= min_tradeable_confidence:   # spec: "Below 60 = No Trade"
                    signal = _build_signal("BUY", entry, sl, range1, confidence, broken_r_idx, level)

        if signal is None:
            broken_s_idx = None
            for i, level in enumerate(supports):
                if latest["close"] < level and (prev_close is None or prev_close >= level):
                    broken_s_idx = i
            if broken_s_idx is not None:
                level = supports[broken_s_idx]
                if (volume_confirmed and trend == "bearish"
                        and not _is_false_breakout(latest, level, "bearish")):
                    entry = latest["close"]
                    # SL = nearest ladder rung strictly above entry, by PRICE --
                    # symmetric reasoning to the buy side (S2 sits BELOW S3).
                    above = [s for s in supports if s > entry]
                    sl = min(above) if above else levels["pdh"]
                    confidence = compute_confidence(latest, level, range1, volume, avg_vol, trend, "bearish", roc)
                    if confidence >= min_tradeable_confidence:   # spec: "Below 60 = No Trade"
                        signal = _build_signal("SELL", entry, sl, range1, confidence, broken_s_idx, level)

    return {
        "pdh": round(levels["pdh"], 2), "pdl": round(levels["pdl"], 2),
        "range": round(levels["range"], 2), "range1": round(levels["range1"], 2),
        "range2": round(levels["range2"], 2),
        "resistances": [round(r, 2) for r in resistances],
        "supports": [round(s, 2) for s in supports],
        "current_price": round(current_price, 2), "trend": trend,
        "avg_volume": avg_vol, "volume": volume, "signal": signal,
        "last_updated": dt.datetime.now().isoformat(timespec="seconds"),
    }
