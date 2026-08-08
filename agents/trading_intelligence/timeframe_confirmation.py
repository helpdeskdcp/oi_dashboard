"""
agents/trading_intelligence/timeframe_confirmation.py -- Milestone 11,
Module 11.2: Multi-Timeframe Probability Engine.

Milestone 10's multi_timeframe.py is PURELY a data-availability module
(get_timeframe()/synchronize() resample the native 3m archive into
15m/30m/1h/daily) -- nothing anywhere in this repository checks whether a
signal actually AGREES with what a higher timeframe is doing. This module
adds exactly that, reusing multi_timeframe.py's own already-derived
candles rather than a second resampling path:

For a given (symbol, direction) -- direction being "CE" (bullish) or "PE"
(bearish), the same two values oi_engine.generate_signal() already
produces -- this module reads each of multi_timeframe.DERIVABLE_TIMEFRAMES
(15m/30m/1h; 1m/5m are correctly never touched here either, since
multi_timeframe.py itself already reports them honestly unavailable) and
asks: over the last TREND_LOOKBACK_BARS bars, did this timeframe's own
close move up, down, or flat? "Up" agrees with CE, "down" agrees with PE,
"flat" agrees with neither (an honest non-signal, not fabricated
agreement). The resulting alignment_score is a plain ratio -- agreeing
timeframes over available timeframes -- never a fitted or weighted model.

Never fabricates a reading: a timeframe that's unavailable (multi_timeframe.
py's own honest 1m/5m gap) or simply doesn't have enough bars yet for
TREND_LOOKBACK_BARS is excluded from both the numerator and denominator,
with the reason recorded per-timeframe, not silently dropped.
"""
import dataclasses
import logging

from . import multi_timeframe

logger = logging.getLogger("oi_dashboard.trading_intelligence.timeframe_confirmation")

# Bars of history compared (most-recent close vs. the close TREND_LOOKBACK_BARS
# bars earlier) to read one timeframe's own short-term trend direction --
# the same bar-count-based (not calendar-time-based) convention
# ai_trading_engine._price_trend_pct() already uses for the native 3m
# candles, applied here per-timeframe so each of 15m/30m/1h gets a
# proportionally similar "recent window" in bar-count terms.
TREND_LOOKBACK_BARS = 4

# Same top-third/bottom-third percentile split regime_profile.py already
# uses for volatility banding (VOLATILITY_HIGH_PERCENTILE/
# VOLATILITY_LOW_PERCENTILE) -- reused here for consistency across the
# two Milestone 11 modules rather than a second, arbitrary threshold pair.
ALIGNMENT_CONFIRMED_PERCENTILE = 200 / 3  # roughly >= 2 of 3 available timeframes agree
ALIGNMENT_CONTRARY_PERCENTILE = 100 / 3   # roughly <= 1 of 3 (or none) agree

VALID_DIRECTIONS = ("CE", "PE")


@dataclasses.dataclass
class TimeframeReading:
    timeframe: str
    available: bool  # True only if BOTH the raw candles existed AND there were enough bars for a trend read
    trend: str  # "UP" | "DOWN" | "FLAT" | "UNKNOWN"
    agrees: bool | None  # None when trend is FLAT/UNKNOWN -- neither confirms nor contradicts, never guessed
    reason: str | None  # populated only when available is False


@dataclasses.dataclass
class TimeframeAlignment:
    symbol: str
    direction: str  # "CE" | "PE"
    readings: list  # list[TimeframeReading], one per multi_timeframe.DERIVABLE_TIMEFRAMES entry
    available_count: int
    agreement_count: int
    alignment_score: float | None  # 0-100: agreement_count / available_count * 100; None if available_count == 0
    alignment_label: str  # "CONFIRMED" | "MIXED" | "CONTRARY" | "UNKNOWN"


def _bar_trend(candles, *, lookback: int = TREND_LOOKBACK_BARS) -> str:
    """"UP"/"DOWN"/"FLAT" from real close-over-close movement across the
    last `lookback` bars of an already-derived candle DataFrame (the
    exact shape multi_timeframe.get_timeframe()'s own "candles" key
    returns) -- "UNKNOWN" if there aren't enough bars yet, the starting
    close is falsy, or either close is NaN (a data-quality gap in the
    archived candle file -- `not start` alone does NOT catch this, since
    NaN is truthy in Python; every NaN comparison below would otherwise
    evaluate False and silently fall through to "FLAT", diluting
    alignment_score's denominator with a reading that was never really
    available -- Milestone 11 Phase 7 validation fix). No threshold/
    noise-band on the magnitude of the move -- any real, nonzero
    close-over-close difference counts as a direction; only a genuinely
    unchanged close is "FLAT". This keeps the function free of an
    arbitrary "how big a move counts" constant."""
    if candles is None or candles.empty or len(candles) < lookback + 1:
        return "UNKNOWN"
    recent = candles.tail(lookback + 1)
    start, end = recent.iloc[0]["close"], recent.iloc[-1]["close"]
    if not start or start != start or end != end:  # `x != x` is the NaN self-inequality check
        return "UNKNOWN"
    if end > start:
        return "UP"
    if end < start:
        return "DOWN"
    return "FLAT"


def check(symbol: str, *, direction: str) -> TimeframeAlignment:
    """The full Module 11.2 read for one (symbol, direction) pair. Never
    raises -- every timeframe that's unavailable or too short for a trend
    read degrades to an honest, recorded "unavailable" reading rather
    than a fabricated agreement/disagreement, the same contract every
    data-reading function in this package already holds to.

    `direction` must be "CE" or "PE" -- the same two values
    oi_engine.generate_signal()'s own "direction" field already produces;
    a caller (ai_trading_engine.py, once wired in) passes the signal's
    own direction straight through, never re-derives it here."""
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"direction must be one of {VALID_DIRECTIONS}, got {direction!r}")

    readings = []
    for tf in multi_timeframe.DERIVABLE_TIMEFRAMES:
        result = multi_timeframe.get_timeframe(symbol, tf)
        if not result["available"]:
            readings.append(TimeframeReading(
                timeframe=tf, available=False, trend="UNKNOWN", agrees=None, reason=result["reason"],
            ))
            continue

        trend = _bar_trend(result["candles"])
        if trend == "UNKNOWN":
            readings.append(TimeframeReading(
                timeframe=tf, available=False, trend="UNKNOWN", agrees=None,
                reason=f"not enough {tf} bars yet for a {TREND_LOOKBACK_BARS}-bar trend read",
            ))
            continue

        agrees = None
        if trend != "FLAT":
            agrees = (direction == "CE" and trend == "UP") or (direction == "PE" and trend == "DOWN")
        readings.append(TimeframeReading(timeframe=tf, available=True, trend=trend, agrees=agrees, reason=None))

    available_count = sum(1 for r in readings if r.available)
    agreement_count = sum(1 for r in readings if r.agrees is True)

    if available_count == 0:
        alignment_score, alignment_label = None, "UNKNOWN"
        logger.debug("timeframe_confirmation: no derivable timeframe available for %s -- alignment UNKNOWN", symbol)
    else:
        alignment_score = round(agreement_count / available_count * 100, 1)
        if alignment_score >= ALIGNMENT_CONFIRMED_PERCENTILE:
            alignment_label = "CONFIRMED"
        elif alignment_score <= ALIGNMENT_CONTRARY_PERCENTILE:
            alignment_label = "CONTRARY"
        else:
            alignment_label = "MIXED"

    return TimeframeAlignment(
        symbol=symbol, direction=direction, readings=readings,
        available_count=available_count, agreement_count=agreement_count,
        alignment_score=alignment_score, alignment_label=alignment_label,
    )
