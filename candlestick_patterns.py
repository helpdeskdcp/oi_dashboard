"""
candlestick_patterns.py -- Rule-based candlestick pattern recognition.

Pure geometric analysis of OHLC candles (open/high/low/close relationships)
-- NOT machine learning. Every pattern here follows the standard, textbook
technical-analysis definition, so results are fully deterministic and
explainable (important for a professional/regulatory-facing demo: no
"black box" claims, no self-tuning thresholds, no reinforcement learning).

Deliberately does NOT include:
- Historical success-rate / accuracy self-learning (see project notes --
  premature with only a few weeks of data; a separate, later addition once
  genuinely enough closed-trade history exists).
- Any prediction of future price movement -- this module only IDENTIFIES
  patterns that already formed; it does not claim what happens next.

USAGE:
    from candlestick_patterns import detect_patterns
    patterns = detect_patterns(candles)   # candles: [{time, open, high, low, close}, ...]
    # patterns: [{"time": ..., "pattern": "Bullish Engulfing", "direction": "bullish"}, ...]
"""

# Tolerance for "near enough to equal" comparisons (e.g. tweezer highs/lows,
# doji open≈close) -- expressed as a fraction of that candle's range.
DOJI_BODY_RATIO = 0.1        # body must be <= 10% of the day's range to count as a doji
SMALL_BODY_RATIO = 0.3       # "small body" threshold for hammer/star-family patterns
MARUBOZU_WICK_RATIO = 0.05   # wick must be <= 5% of the day's range to count as a marubozu
TWEEZER_TOLERANCE = 0.001    # 0.1% tolerance for "matching" highs/lows


def _candle_metrics(c):
    o, h, l, cl = c["open"], c["high"], c["low"], c["close"]
    rng = h - l
    body = abs(cl - o)
    upper_wick = h - max(o, cl)
    lower_wick = min(o, cl) - l
    is_bullish = cl > o
    return {
        "open": o, "high": h, "low": l, "close": cl, "range": rng, "body": body,
        "upper_wick": upper_wick, "lower_wick": lower_wick, "is_bullish": is_bullish,
        "mid": (o + cl) / 2,
    }


def _is_doji(m):
    return m["range"] > 0 and m["body"] / m["range"] <= DOJI_BODY_RATIO


def _is_marubozu(m):
    return m["range"] > 0 and m["upper_wick"] / m["range"] <= MARUBOZU_WICK_RATIO and m["lower_wick"] / m["range"] <= MARUBOZU_WICK_RATIO and m["body"] / m["range"] > 0.9


def _is_hammer_shape(m):
    """Small body near the TOP of the range, long lower wick, small/no upper wick."""
    return (m["range"] > 0 and m["body"] / m["range"] <= SMALL_BODY_RATIO
            and m["lower_wick"] >= 2 * m["body"] and m["upper_wick"] <= m["body"])


def _is_star_shape(m):
    """Small body near the BOTTOM of the range, long upper wick, small/no lower wick."""
    return (m["range"] > 0 and m["body"] / m["range"] <= SMALL_BODY_RATIO
            and m["upper_wick"] >= 2 * m["body"] and m["lower_wick"] <= m["body"])


def detect_patterns(candles):
    """
    Scans a list of OHLC candles and returns detected patterns, each tagged
    with the candle-time they occurred at (the LAST candle in a multi-candle
    pattern) and a bullish/bearish/neutral direction-label.

    candles: [{"time": <unix_seconds>, "open": .., "high": .., "low": .., "close": ..}, ...]
    Returns: [{"time": .., "pattern": <name>, "direction": "bullish"|"bearish"|"neutral"}, ...]
    """
    results = []
    n = len(candles)
    metrics = [_candle_metrics(c) for c in candles]

    for i in range(n):
        m = metrics[i]
        t = candles[i]["time"]

        # -- Single-candle patterns --
        if _is_doji(m):
            results.append({"time": t, "pattern": "Doji", "direction": "neutral"})
        if _is_marubozu(m):
            results.append({"time": t, "pattern": "Marubozu", "direction": "bullish" if m["is_bullish"] else "bearish"})

        # Hammer / Shooting Star / Inverted Hammer -- shape is the same
        # (small body + long wick on one side); direction depends on the
        # preceding short-term trend (need at least 3 prior candles).
        if i >= 3:
            prior_trend_down = candles[i - 1]["close"] < candles[i - 3]["close"]
            prior_trend_up = candles[i - 1]["close"] > candles[i - 3]["close"]
            if _is_hammer_shape(m) and prior_trend_down:
                results.append({"time": t, "pattern": "Hammer", "direction": "bullish"})
            if _is_star_shape(m) and prior_trend_down:
                results.append({"time": t, "pattern": "Inverted Hammer", "direction": "bullish"})
            if _is_star_shape(m) and prior_trend_up:
                results.append({"time": t, "pattern": "Shooting Star", "direction": "bearish"})

        # -- Two-candle patterns --
        if i >= 1:
            prev = metrics[i - 1]

            # Bullish / Bearish Engulfing: current body fully engulfs previous body, opposite colors
            if m["is_bullish"] and not prev["is_bullish"] and m["open"] <= prev["close"] and m["close"] >= prev["open"]:
                results.append({"time": t, "pattern": "Bullish Engulfing", "direction": "bullish"})
            if not m["is_bullish"] and prev["is_bullish"] and m["open"] >= prev["close"] and m["close"] <= prev["open"]:
                results.append({"time": t, "pattern": "Bearish Engulfing", "direction": "bearish"})

            # Harami: current body fully INSIDE previous body, opposite colors
            if (prev["body"] > 0 and m["body"] < prev["body"]
                    and max(m["open"], m["close"]) < max(prev["open"], prev["close"])
                    and min(m["open"], m["close"]) > min(prev["open"], prev["close"])):
                results.append({"time": t, "pattern": "Harami", "direction": "bullish" if m["is_bullish"] else "bearish"})

            # Piercing Pattern: bearish then bullish, opens below prior low, closes above prior body's midpoint
            if (not prev["is_bullish"] and m["is_bullish"]
                    and m["open"] < prev["low"]
                    and m["close"] > prev["mid"] and m["close"] < prev["open"]):
                results.append({"time": t, "pattern": "Piercing Pattern", "direction": "bullish"})

            # Dark Cloud Cover: bullish then bearish, opens above prior high, closes below prior body's midpoint
            if (prev["is_bullish"] and not m["is_bullish"]
                    and m["open"] > prev["high"]
                    and m["close"] < prev["mid"] and m["close"] > prev["open"]):
                results.append({"time": t, "pattern": "Dark Cloud Cover", "direction": "bearish"})

            # Tweezer Top / Bottom: matching highs (top, reversal-down) or matching
            # lows (bottom, reversal-up). Requires a preceding trend in the
            # DIRECTION being reversed (same "need at least 3 prior candles" rule
            # as Hammer/Star above) -- without it, two similar-range candles in a
            # flat/choppy stretch could satisfy BOTH the high-match and low-match
            # tolerance checks at once and fire contradictory bearish+bullish
            # signals off the same candle pair. elif also makes the two mutually
            # exclusive on any single pair.
            if i >= 3:
                tweezer_prior_up = candles[i - 1]["close"] > candles[i - 3]["close"]
                tweezer_prior_down = candles[i - 1]["close"] < candles[i - 3]["close"]
                matches_high = prev["high"] > 0 and abs(m["high"] - prev["high"]) / prev["high"] <= TWEEZER_TOLERANCE
                matches_low = prev["low"] > 0 and abs(m["low"] - prev["low"]) / prev["low"] <= TWEEZER_TOLERANCE
                if tweezer_prior_up and matches_high:
                    results.append({"time": t, "pattern": "Tweezer Top", "direction": "bearish"})
                elif tweezer_prior_down and matches_low:
                    results.append({"time": t, "pattern": "Tweezer Bottom", "direction": "bullish"})

        # -- Three-candle patterns --
        if i >= 2:
            c1, c2, c3 = metrics[i - 2], metrics[i - 1], m

            # Morning Star: bearish, small-body (gap down), bullish closing well into candle-1's body
            if (not c1["is_bullish"] and c1["body"] > 0
                    and c2["body"] / max(c1["body"], 1e-9) <= SMALL_BODY_RATIO
                    and c3["is_bullish"] and c3["close"] > c1["mid"]):
                results.append({"time": t, "pattern": "Morning Star", "direction": "bullish"})

            # Evening Star: bullish, small-body (gap up), bearish closing well into candle-1's body
            if (c1["is_bullish"] and c1["body"] > 0
                    and c2["body"] / max(c1["body"], 1e-9) <= SMALL_BODY_RATIO
                    and not c3["is_bullish"] and c3["close"] < c1["mid"]):
                results.append({"time": t, "pattern": "Evening Star", "direction": "bearish"})

            # Three White Soldiers: 3 consecutive bullish candles, each opening within
            # previous body and closing higher than previous close
            if (c1["is_bullish"] and c2["is_bullish"] and c3["is_bullish"]
                    and c2["open"] > c1["open"] and c2["open"] < c1["close"]
                    and c3["open"] > c2["open"] and c3["open"] < c2["close"]
                    and c2["close"] > c1["close"] and c3["close"] > c2["close"]):
                results.append({"time": t, "pattern": "Three White Soldiers", "direction": "bullish"})

            # Three Black Crows: mirror of the above
            if (not c1["is_bullish"] and not c2["is_bullish"] and not c3["is_bullish"]
                    and c2["open"] < c1["open"] and c2["open"] > c1["close"]
                    and c3["open"] < c2["open"] and c3["open"] > c2["close"]
                    and c2["close"] < c1["close"] and c3["close"] < c2["close"]):
                results.append({"time": t, "pattern": "Three Black Crows", "direction": "bearish"})

    return results
