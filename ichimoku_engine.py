#!/usr/bin/env python3
"""
ichimoku_engine.py -- Institutional Ichimoku Kinko Hyo engine.

NOT a TradingView clone: every component below is computed directly from
OHLC candle data using the textbook formulas (documented per-function), then
combined into structured trend/cloud/momentum/signal/risk output the rest of
this dashboard's engines (oi_engine.py, market_structure.py, backtest.py,
app.py) can consume the same way they consume each other -- same candle
dict shape ({'datetime','open','high','low','close','volume'}, oldest-first)
as market_structure.py, same "dict of plain numbers/strings, no black-box
score" return convention as oi_engine.py's compute_new_trend_meter.

ADVISORY ONLY as shipped: this engine's BUY/SELL/STRONG BUY/STRONG SELL
output does NOT place or gate any real order on its own (see app.py's
wiring -- it is computed and displayed/logged alongside the existing
engines, exactly like S/R Engine V3 and the Scalping Engine were before
they earned a track record). A live-execution feature flag is planned once
there's a large enough logged sample (see log_recommendation_outcome-style
hooks in backtest.py/app.py) to measure win rate / precision / false-buy%
/ false-sell% / avg R:R / drawdown / trend-detection accuracy.

Formula reference (standard Ichimoku Kinko Hyo, Goichi Hosoda):
  Tenkan-sen   (Conversion Line) = (Highest High(9)  + Lowest Low(9))  / 2
  Kijun-sen    (Base Line)       = (Highest High(26) + Lowest Low(26)) / 2
  Senkou Span A (Leading Span A) = (Tenkan + Kijun) / 2,   plotted +26 ahead
  Senkou Span B (Leading Span B) = (Highest High(52) + Lowest Low(52)) / 2, plotted +26 ahead
  Chikou Span  (Lagging Span)    = Close,                  plotted -26 behind
No proprietary math -- same "standard, documented convention" rule
market_structure.py's classical_pivots/calc_cpr already follow.

Candles: underlying spot/futures OHLCV (same series market_structure.py's
ATR/ADX/VWAP already run on), NOT option premium. Risk-management figures
(entry/stop/targets) below are therefore in UNDERLYING POINTS, matching
market_structure.py's ATR/pivot convention -- converting to option-premium
terms is the caller's job (oi_engine.py already does this via Greeks/delta
for its own signal engine).

Supported timeframes (engine accepts candles at ANY of these; Angel One's
broker feed itself only actually supplies ONE_MINUTE/THREE_MINUTE/
FIVE_MINUTE/TEN_MINUTE/FIFTEEN_MINUTE/THIRTY_MINUTE/ONE_HOUR/ONE_DAY today
-- sub-minute timeframes are accepted by this engine's API for
future/other-data-source use but are not currently fetchable live):
  1s 5s 15s 30s 1m 2m 3m 5m 10m 15m 30m 1h 4h 1d

History requirement: minimum 120 candles (senkou_b lookback 52 + 26
displacement + buffer), 300-500 recommended so Tenkan/Kijun/cloud values
near the start of the window aren't NaN-starved. Fewer candles degrades
gracefully (whatever components have enough history are populated, the
rest report None) rather than raising.
"""

from __future__ import annotations

import datetime as dt
from collections import deque
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np
import pandas as pd

from market_structure import calc_adx, calc_atr, calc_vwap

# ----------------------------------------------------------------------------
# Institutional defaults -- every one is a function parameter (configurable),
# these are just the standard/sane starting point (textbook Ichimoku periods
# for the four period params; the rest are this dashboard's own choices,
# flagged as such below, same honesty convention as oi_engine.py's
# compute_new_trend_meter "weights not yet empirically validated" note).
# ----------------------------------------------------------------------------
TENKAN_PERIOD = 9
KIJUN_PERIOD = 26
SENKOU_B_PERIOD = 52
DISPLACEMENT = 26

MIN_CANDLES = 120
RECOMMENDED_CANDLES = 300

SUPPORTED_TIMEFRAMES = (
    "1s", "5s", "15s", "30s", "1m", "2m", "3m", "5m", "10m", "15m", "30m", "1h", "4h", "1d",
)

# Not empirically backtested yet -- starting values only, see module docstring.
CLOUD_THIN_ATR_MULT = 0.3          # cloud thinner than this x ATR = "thin"/weak, not "acceptable"
BREAKOUT_LOOKBACK = 3              # candles to look back for "just crossed the cloud" (breakout/breakdown)
REJECTION_LOOKBACK = 5             # candles to look back for a touch-and-reverse at the cloud
EXHAUSTION_ATR_MULT = 2.5          # price this many ATRs from Kijun = extended/exhausted
STRONG_SIGNAL_MIN_CONDITIONS = 7   # of 9 checklist conditions -- "multiple modules align"
BUY_SIGNAL_MIN_CONDITIONS = 5
DEFAULT_RISK_PER_TRADE_PCT = 1.0   # used only if caller supplies `capital`


def _candles_to_df(candles) -> pd.DataFrame:
    """
    Normalizes either this codebase's list[dict] candle convention
    (market_structure.py / app.py's Angel One fetch) or a pandas DataFrame
    (backtest.py's parquet/csv archive convention) into one canonical
    DataFrame, oldest-first, indexed 0..n-1. Never mutates the caller's
    object.
    """
    if isinstance(candles, pd.DataFrame):
        df = candles.copy()
    else:
        df = pd.DataFrame(list(candles))
    if df.empty:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    if "datetime" in df.columns:
        df["datetime"] = pd.to_datetime(df["datetime"])
        df = df.sort_values("datetime").reset_index(drop=True)
    if "volume" not in df.columns:
        df["volume"] = 0.0
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _ema(series: pd.Series, span: int) -> pd.Series:
    """Standard exponential moving average. No existing EMA helper elsewhere
    in this codebase (sr_probability_engine's compute_premium_ema is
    premium-specific) -- this is the generic price-series version."""
    return series.ewm(span=span, adjust=False).mean()


# ----------------------------------------------------------------------------
# Structured output types
# ----------------------------------------------------------------------------

@dataclass
class IchimokuValues:
    tenkan: Optional[float]
    kijun: Optional[float]
    senkou_a: Optional[float]
    senkou_b: Optional[float]
    chikou: Optional[float]


@dataclass
class CloudValues:
    top: Optional[float]
    bottom: Optional[float]
    thickness: Optional[float]
    direction: str   # "Bullish" | "Bearish" | "Neutral"


def calculate_ichimoku(
    candles,
    tenkan_period: int = TENKAN_PERIOD,
    kijun_period: int = KIJUN_PERIOD,
    senkou_b_period: int = SENKOU_B_PERIOD,
    displacement: int = DISPLACEMENT,
) -> pd.DataFrame:
    """
    Vectorized bulk calculation (pandas .rolling(), O(n) not O(n*period) --
    the "avoid recalculating historical candles repeatedly" requirement at
    the ALGORITHMIC level; see IchimokuLiveEngine below for the streaming/
    incremental-update requirement at the PER-TICK level).

    Returns the input candles plus columns: tenkan, kijun, senkou_a,
    senkou_b, chikou, future_senkou_a, future_senkou_b.

    senkou_a/senkou_b are ALIGNED to the candle they apply to (i.e. already
    shifted +displacement so `df.iloc[i].senkou_a` is the cloud boundary
    that candle i's price should be compared against -- the value that was
    computed `displacement` candles ago and is only now "plotted"/active).

    future_senkou_a/future_senkou_b are the UN-shifted values -- the cloud
    that will become active `displacement` candles from now -- used for the
    "Future Cloud Direction" / "Cloud Twist" checks.

    chikou is the Lagging Span aligned the same way: `df.iloc[i].chikou`
    is what's plotted AT position i, i.e. close[i + displacement] -- NaN
    for the most recent `displacement` candles (that data doesn't exist
    yet), which is correct/expected, not a bug.
    """
    df = _candles_to_df(candles)
    if df.empty:
        return df

    high, low, close = df["high"], df["low"], df["close"]

    tenkan = (high.rolling(tenkan_period).max() + low.rolling(tenkan_period).min()) / 2
    kijun = (high.rolling(kijun_period).max() + low.rolling(kijun_period).min()) / 2
    raw_senkou_a = (tenkan + kijun) / 2
    raw_senkou_b = (high.rolling(senkou_b_period).max() + low.rolling(senkou_b_period).min()) / 2

    df["tenkan"] = tenkan
    df["kijun"] = kijun
    df["senkou_a"] = raw_senkou_a.shift(displacement)
    df["senkou_b"] = raw_senkou_b.shift(displacement)
    df["future_senkou_a"] = raw_senkou_a
    df["future_senkou_b"] = raw_senkou_b
    df["chikou"] = close.shift(-displacement)
    return df


# ----------------------------------------------------------------------------
# Cloud / trend analysis
# ----------------------------------------------------------------------------

def cloud_values_at(df: pd.DataFrame, idx: int = -1) -> CloudValues:
    """Cloud Top/Bottom/Thickness/Direction at a given row (default: latest)."""
    if df.empty:
        return CloudValues(None, None, None, "Neutral")
    row = df.iloc[idx]
    a, b = row.get("senkou_a"), row.get("senkou_b")
    if pd.isna(a) or pd.isna(b):
        return CloudValues(None, None, None, "Neutral")
    top, bottom = max(a, b), min(a, b)
    thickness = abs(a - b)
    if a > b:
        direction = "Bullish"
    elif a < b:
        direction = "Bearish"
    else:
        direction = "Neutral"
    return CloudValues(round(top, 2), round(bottom, 2), round(thickness, 2), direction)


def price_vs_cloud(price: float, cloud: CloudValues) -> str:
    """'Above Cloud' | 'Below Cloud' | 'Inside Cloud' | 'Unknown' (cloud not ready yet)."""
    if cloud.top is None or cloud.bottom is None:
        return "Unknown"
    if price > cloud.top:
        return "Above Cloud"
    if price < cloud.bottom:
        return "Below Cloud"
    return "Inside Cloud"


def detect_cloud_events(df: pd.DataFrame, lookback_breakout: int = BREAKOUT_LOOKBACK,
                         lookback_rejection: int = REJECTION_LOOKBACK) -> dict:
    """
    Cloud Breakout / Cloud Breakdown / Cloud Rejection / Cloud Bounce /
    Cloud Twist / Future Cloud Direction, read off the already-vectorized
    dataframe (no recomputation of tenkan/kijun/cloud here).

    - Breakout: price closed inside-or-below the cloud within the last
      `lookback_breakout` candles and the LATEST close is above cloud top.
    - Breakdown: mirror image (was inside-or-above, now below cloud bottom).
    - Rejection: price touched/entered the cloud from above within
      `lookback_rejection` candles then closed back above it (failed
      breakdown attempt -- cloud acted as support) -- or the bearish mirror
      (touched from below, rejected back down -- cloud acted as resistance).
    - Bounce: alias for the same touch-and-reverse behavior stated from the
      "price bounced off the cloud" perspective (kept as a separate label
      since both terms are standard Ichimoku vocabulary and callers may
      look for either).
    - Twist: senkou_a/senkou_b (future cloud, i.e. `future_senkou_a` /
      `future_senkou_b`) cross within the projection window -- the cloud
      itself is about to flip color, a classic leading reversal warning.
    - Future Cloud Direction: color of the cloud `displacement` candles
      from now (already known today, since Senkou A/B are pre-computed).
    """
    out = {
        "price_vs_cloud": "Unknown", "cloud_breakout": False, "cloud_breakdown": False,
        "cloud_rejection": None, "cloud_bounce": None, "cloud_twist": False,
        "future_cloud_direction": "Neutral",
    }
    if df.empty or len(df) < 2:
        return out

    cloud = cloud_values_at(df, -1)
    close = df["close"]
    price = close.iloc[-1]
    out["price_vs_cloud"] = price_vs_cloud(price, cloud)

    top, bottom = df["senkou_a"].combine(df["senkou_b"], max), df["senkou_a"].combine(df["senkou_b"], min)
    recent_top = top.iloc[-(lookback_breakout + 1):-1]
    recent_bottom = bottom.iloc[-(lookback_breakout + 1):-1]
    recent_close = close.iloc[-(lookback_breakout + 1):-1]
    if not recent_close.empty and cloud.top is not None:
        was_not_above = (recent_close <= recent_top).any()
        out["cloud_breakout"] = bool(was_not_above and price > cloud.top)
        was_not_below = (recent_close >= recent_bottom).any()
        out["cloud_breakdown"] = bool(was_not_below and price < cloud.bottom)

    # Rejection/bounce: over the rejection lookback window, did price dip
    # into-or-below the cloud (from an above-cloud stance) and then close
    # back above it -- or the bearish mirror?
    window = min(lookback_rejection + 1, len(df))
    tail = df.iloc[-window:]
    if cloud.top is not None and len(tail) >= 2:
        touched_from_above = ((tail["close"].iloc[:-1] > tail["senkou_a"].combine(tail["senkou_b"], max).iloc[:-1]) &
                               (tail["low"].iloc[:-1] <= tail["senkou_a"].combine(tail["senkou_b"], max).iloc[:-1])).any()
        if touched_from_above and price > cloud.top:
            out["cloud_rejection"] = "Bullish (cloud held as support)"
            out["cloud_bounce"] = "Bullish"
        touched_from_below = ((tail["close"].iloc[:-1] < tail["senkou_a"].combine(tail["senkou_b"], min).iloc[:-1]) &
                               (tail["high"].iloc[:-1] >= tail["senkou_a"].combine(tail["senkou_b"], min).iloc[:-1])).any()
        if touched_from_below and price < cloud.bottom:
            out["cloud_rejection"] = "Bearish (cloud held as resistance)"
            out["cloud_bounce"] = "Bearish"

    fa, fb = df["future_senkou_a"], df["future_senkou_b"]
    if len(fa.dropna()) >= 2 and len(fb.dropna()) >= 2:
        cur_sign = np.sign(fa.iloc[-1] - fb.iloc[-1]) if not (pd.isna(fa.iloc[-1]) or pd.isna(fb.iloc[-1])) else 0
        prev_valid = fa.dropna().index[-2] if len(fa.dropna()) >= 2 else None
        if prev_valid is not None and prev_valid in fb.dropna().index:
            prev_sign = np.sign(fa.loc[prev_valid] - fb.loc[prev_valid])
            out["cloud_twist"] = bool(cur_sign != 0 and prev_sign != 0 and cur_sign != prev_sign)
        out["future_cloud_direction"] = (
            "Bullish" if cur_sign > 0 else "Bearish" if cur_sign < 0 else "Neutral"
        )
    return out


def tenkan_kijun_cross(df: pd.DataFrame, cloud: Optional[CloudValues] = None) -> dict:
    """
    Bullish Cross / Bearish Cross (Tenkan crosses Kijun this candle),
    Golden Cross / Death Cross (same cross, but classified by WHERE it
    happens -- Golden Cross = bullish cross occurring above or entering
    the cloud, i.e. the high-conviction version; Death Cross = bearish
    cross below/entering the cloud -- standard Ichimoku terminology
    distinguishing a plain TK cross from one confirmed by cloud position),
    Cross Strength (0-100, scaled by how far apart Tenkan/Kijun are moving
    relative to ATR -- a cross with strong separation is more decisive
    than one where the lines barely cross), and where the cross happened
    relative to the cloud.
    """
    out = {"bullish_cross": False, "bearish_cross": False, "golden_cross": False,
           "death_cross": False, "cross_strength": 0.0, "cross_location": None}
    if len(df) < 2:
        return out
    t, k = df["tenkan"], df["kijun"]
    if pd.isna(t.iloc[-1]) or pd.isna(k.iloc[-1]) or pd.isna(t.iloc[-2]) or pd.isna(k.iloc[-2]):
        return out

    prev_diff, cur_diff = t.iloc[-2] - k.iloc[-2], t.iloc[-1] - k.iloc[-1]
    out["bullish_cross"] = bool(prev_diff <= 0 and cur_diff > 0)
    out["bearish_cross"] = bool(prev_diff >= 0 and cur_diff < 0)

    cloud = cloud or cloud_values_at(df, -1)
    price = df["close"].iloc[-1]
    location = price_vs_cloud(price, cloud)
    out["cross_location"] = location
    if out["bullish_cross"]:
        out["golden_cross"] = location in ("Above Cloud", "Inside Cloud")
    if out["bearish_cross"]:
        out["death_cross"] = location in ("Below Cloud", "Inside Cloud")

    if out["bullish_cross"] or out["bearish_cross"]:
        atr_series = df["high"].sub(df["low"]).rolling(14).mean()
        atr = atr_series.iloc[-1]
        if atr and atr > 0 and not pd.isna(atr):
            out["cross_strength"] = round(min(100.0, abs(cur_diff) / atr * 100), 1)
    return out


def chikou_analysis(df: pd.DataFrame, displacement: int = DISPLACEMENT) -> dict:
    """
    Chikou (lagging span, = current close) vs the price that was actually
    trading `displacement` candles ago -- Above/Below/Inside, plus Bullish/
    Bearish Confirmation (Chikou position agrees with the current
    price-vs-cloud stance, the standard Ichimoku "triple confirmation"
    check: cloud + TK cross + Chikou all pointing the same way).
    """
    out = {"chikou_vs_price": "Unknown", "bullish_confirmation": False, "bearish_confirmation": False}
    if len(df) <= displacement:
        return out
    close = df["close"]
    chikou_value = close.iloc[-1]                      # today's close, "plotted" displacement candles back
    ref_high, ref_low = df["high"].iloc[-1 - displacement], df["low"].iloc[-1 - displacement]
    if chikou_value > ref_high:
        out["chikou_vs_price"] = "Above Price"
    elif chikou_value < ref_low:
        out["chikou_vs_price"] = "Below Price"
    else:
        out["chikou_vs_price"] = "Inside Price"

    cloud = cloud_values_at(df, -1)
    stance = price_vs_cloud(close.iloc[-1], cloud)
    out["bullish_confirmation"] = out["chikou_vs_price"] == "Above Price" and stance == "Above Cloud"
    out["bearish_confirmation"] = out["chikou_vs_price"] == "Below Price" and stance == "Below Cloud"
    return out


def dynamic_support_resistance(df: pd.DataFrame, price: float, atr: Optional[float] = None) -> dict:
    """
    Cloud (+ Tenkan/Kijun) as dynamic support/resistance. Candidate levels:
    cloud top, cloud bottom, tenkan, kijun. Nearest level BELOW price =
    support, nearest ABOVE = resistance. Strength is a 0-100 read combining
    (a) confluence -- how many of the 4 candidate levels sit within one ATR
    of each other (stacked levels = stronger, standard S/R-confluence
    logic already used by market_structure.cross_verify_wall) and
    (b) cloud thickness relative to ATR (a thick cloud is a more durable
    barrier than a thin one).
    """
    out = {"nearest_support": None, "nearest_resistance": None,
           "distance_from_support": None, "distance_from_resistance": None,
           "support_strength": 0, "resistance_strength": 0}
    if df.empty:
        return out
    row = df.iloc[-1]
    cloud = cloud_values_at(df, -1)
    candidates = [v for v in (cloud.top, cloud.bottom, row.get("tenkan"), row.get("kijun")) if v is not None and not pd.isna(v)]
    if not candidates:
        return out

    below = [v for v in candidates if v <= price]
    above = [v for v in candidates if v > price]
    support = max(below) if below else None
    resistance = min(above) if above else None

    atr = atr if atr and atr > 0 else 1.0
    def strength(level):
        if level is None:
            return 0
        confluence = sum(1 for v in candidates if abs(v - level) <= atr * 0.25)
        thickness_bonus = 20 if cloud.thickness and cloud.thickness > atr * CLOUD_THIN_ATR_MULT else 0
        return min(100, confluence * 25 + thickness_bonus)

    out["nearest_support"] = round(support, 2) if support is not None else None
    out["nearest_resistance"] = round(resistance, 2) if resistance is not None else None
    out["distance_from_support"] = round(price - support, 2) if support is not None else None
    out["distance_from_resistance"] = round(resistance - price, 2) if resistance is not None else None
    out["support_strength"] = strength(support)
    out["resistance_strength"] = strength(resistance)
    return out


def momentum_analysis(df: pd.DataFrame, lookback: int = 5, atr: Optional[float] = None) -> dict:
    """
    Trend Acceleration/Weakening (is the Tenkan-Kijun spread widening or
    narrowing over `lookback` candles), Momentum Increasing/Decreasing
    (rate of change of Tenkan slope), Trend Exhaustion / Possible Reversal
    (price extended > EXHAUSTION_ATR_MULT x ATR away from Kijun -- a
    standard "price is stretched, mean-reversion risk rising" read).
    """
    out = {"trend_acceleration": False, "trend_weakening": False,
           "momentum_increasing": False, "momentum_decreasing": False,
           "trend_exhaustion": False, "possible_reversal": False}
    if len(df) < lookback + 2:
        return out
    spread = (df["tenkan"] - df["kijun"]).dropna()
    if len(spread) < lookback + 1:
        return out

    cur_abs_spread, prev_abs_spread = abs(spread.iloc[-1]), abs(spread.iloc[-1 - lookback])
    out["trend_acceleration"] = bool(cur_abs_spread > prev_abs_spread)
    out["trend_weakening"] = bool(cur_abs_spread < prev_abs_spread)

    tenkan = df["tenkan"].dropna()
    if len(tenkan) >= lookback + 2:
        slope_now = tenkan.iloc[-1] - tenkan.iloc[-2]
        slope_before = tenkan.iloc[-1 - lookback] - tenkan.iloc[-2 - lookback]
        out["momentum_increasing"] = bool(abs(slope_now) > abs(slope_before))
        out["momentum_decreasing"] = bool(abs(slope_now) < abs(slope_before))

    kijun_last = df["kijun"].iloc[-1]
    price = df["close"].iloc[-1]
    if atr and atr > 0 and not pd.isna(kijun_last):
        distance_atr = abs(price - kijun_last) / atr
        out["trend_exhaustion"] = bool(distance_atr > EXHAUSTION_ATR_MULT)
        out["possible_reversal"] = bool(out["trend_exhaustion"] and out["trend_weakening"])
    return out


TREND_LABELS = (
    "Very Strong Bearish", "Strong Bearish", "Bearish", "Neutral",
    "Bullish", "Strong Bullish", "Very Strong Bullish",
)


def classify_trend(cloud_events: dict, cross: dict, chikou: dict, momentum: dict) -> str:
    """
    7-level trend classification from a simple weighted vote across the
    four already-computed analyses above (cloud position, TK cross/
    location, Chikou confirmation, momentum direction) -- score -3..+3
    mapped onto TREND_LABELS. Documented, not a black box: `score` and each
    contributing vote are returned by analyze() alongside the label.
    """
    score = 0
    pos = cloud_events["price_vs_cloud"]
    score += 1 if pos == "Above Cloud" else -1 if pos == "Below Cloud" else 0
    if cross.get("golden_cross"):
        score += 1
    elif cross.get("bullish_cross"):
        score += 0.5
    if cross.get("death_cross"):
        score -= 1
    elif cross.get("bearish_cross"):
        score -= 0.5
    if chikou.get("bullish_confirmation"):
        score += 1
    elif chikou.get("bearish_confirmation"):
        score -= 1
    if cloud_events.get("future_cloud_direction") == "Bullish":
        score += 0.5
    elif cloud_events.get("future_cloud_direction") == "Bearish":
        score -= 0.5
    if momentum.get("possible_reversal"):
        score *= 0.5   # exhaustion pulls the classification back toward Neutral

    if score >= 2.5:
        return "Very Strong Bullish"
    if score >= 1.5:
        return "Strong Bullish"
    if score >= 0.5:
        return "Bullish"
    if score > -0.5:
        return "Neutral"
    if score > -1.5:
        return "Bearish"
    if score > -2.5:
        return "Strong Bearish"
    return "Very Strong Bearish"


TREND_STAGES = (
    "Trend Formation", "Early Trend", "Strong Trend",
    "Mature Trend", "Exhaustion", "Reversal Probability",
)

# Stage -> recommended position-management action (a documented rule
# mapping, not ML -- the AI Decision layer reads this rather than the raw
# stage name so every consumer applies the same institutional playbook).
TREND_STAGE_ACTIONS = {
    "Trend Formation": "Avoid New Entries",
    "Early Trend": "Enter",
    "Strong Trend": "Add Position",
    "Mature Trend": "Hold",
    "Exhaustion": "Take Partial Profit",
    "Reversal Probability": "Exit",
}


def classify_trend_stage(cloud_events: dict, cross: dict, momentum: dict, adx: Optional[float]) -> dict:
    """
    Trend Lifecycle Detection: classifies the CURRENT trend into one of
    TREND_STAGES using signals already computed elsewhere in this module
    (no new indicators) --

      Reversal Probability: momentum.possible_reversal, OR a cloud twist is
        imminent, OR the freshest TK cross opposes the established
        price-vs-cloud stance -- the strongest warning, checked first.
      Exhaustion: price stretched > EXHAUSTION_ATR_MULT x ATR from Kijun
        (momentum.trend_exhaustion) without yet flipping.
      Strong Trend: ADX >= 25 (market_structure.classify_regime's own
        TRENDING threshold) AND momentum.trend_acceleration.
      Mature Trend: ADX >= 25 but momentum.trend_weakening / not
        accelerating -- still trending, but no longer gaining pace.
      Early Trend: cloud/TK-cross direction established, ADX 18-25
        (market_structure's TRANSITIONING band) and momentum increasing.
      Trend Formation: everything else with SOME directional lean (a
        golden/death cross or a recent breakout/breakdown) but ADX still
        low (<18, market_structure's RANGING threshold) -- too early to
        trust yet.
      Falls back to "Trend Formation" if none of the above conditions are
      met but there IS a directional lean, otherwise stage is None (no
      real trend to stage -- e.g. price still inside the cloud).

    Returns {'stage', 'recommended_action', 'reasons'} -- recommended_action
    is TREND_STAGE_ACTIONS[stage], the rule the AI Decision layer (Trading
    Meter / backtest / paper trading -- see oi_engine.compute_new_trend_meter)
    is expected to apply consistently rather than re-deriving its own.
    """
    directional = cloud_events.get("price_vs_cloud") in ("Above Cloud", "Below Cloud")
    if not directional:
        return {"stage": None, "recommended_action": "Avoid New Entries",
                "reasons": ["Price inside the cloud -- no established trend to stage."]}

    reasons = []
    opposite_cross = (
        (cloud_events["price_vs_cloud"] == "Above Cloud" and cross.get("bearish_cross")) or
        (cloud_events["price_vs_cloud"] == "Below Cloud" and cross.get("bullish_cross"))
    )
    if momentum.get("possible_reversal") or cloud_events.get("cloud_twist") or opposite_cross:
        stage = "Reversal Probability"
        if momentum.get("possible_reversal"):
            reasons.append("price extended from Kijun and momentum weakening")
        if cloud_events.get("cloud_twist"):
            reasons.append("cloud twist ahead -- cloud about to flip color")
        if opposite_cross:
            reasons.append("fresh TK cross opposes the established trend")
    elif momentum.get("trend_exhaustion"):
        stage = "Exhaustion"
        reasons.append("price stretched far from Kijun -- trend may be running out of room")
    elif adx is not None and adx >= 25 and momentum.get("trend_acceleration"):
        stage = "Strong Trend"
        reasons.append(f"ADX={adx} (trending) and Tenkan-Kijun spread widening")
    elif adx is not None and adx >= 25:
        stage = "Mature Trend"
        reasons.append(f"ADX={adx} (trending) but momentum no longer accelerating")
    elif adx is not None and adx >= 18 and momentum.get("momentum_increasing"):
        stage = "Early Trend"
        reasons.append(f"ADX={adx} (building) and momentum increasing")
    else:
        stage = "Trend Formation"
        reasons.append("Directional lean established but ADX still low -- unconfirmed")

    return {"stage": stage, "recommended_action": TREND_STAGE_ACTIONS[stage], "reasons": reasons}


# ----------------------------------------------------------------------------
# Signal generator + risk management + confidence
# ----------------------------------------------------------------------------

def generate_ichimoku_signal(
    df: pd.DataFrame,
    adx: Optional[float] = None,
    regime: Optional[str] = None,
    ema_period: int = 50,
    vwap: Optional[float] = None,
    volume_sma_period: int = 20,
    atr: Optional[float] = None,
    resistance_proximity_atr_mult: float = 0.5,
    strong_min_conditions: int = STRONG_SIGNAL_MIN_CONDITIONS,
    buy_min_conditions: int = BUY_SIGNAL_MIN_CONDITIONS,
) -> dict:
    """
    Rule-based BUY / STRONG BUY / SELL / STRONG SELL / WAIT / NO_TRADE.

    NOT a single indicator: counts how many of a 9-point institutional
    checklist agree (price-vs-cloud, Tenkan/Kijun, future cloud color,
    Chikou confirmation, cloud-thickness acceptable, ADX trending, EMA
    trend agreement, Volume confirmation, VWAP agreement, no major
    opposing S/R nearby -- 10 checks total, see `conditions` in the
    return). STRONG BUY/SELL requires >= `strong_min_conditions` of them
    to align in the SAME direction ("multiple institutional modules
    align", never forced from a single indicator); plain BUY/SELL needs
    >= `buy_min_conditions`; anything short of that is WAIT (data present
    but not enough agreement) or NO_TRADE (not enough candle history for
    the cloud to even exist yet).

    adx/regime/vwap/atr are OPTIONAL passthroughs -- reuse
    market_structure.calc_adx/calc_atr/calc_vwap on the SAME candles
    rather than recomputing them here (no duplicate logic). If omitted,
    those specific checklist items simply don't contribute (graceful
    degrade, same convention as market_structure.calc_vwap returning None
    when there's no volume data).
    """
    if df.empty or len(df) < KIJUN_PERIOD + DISPLACEMENT:
        return {"action": "NO_TRADE", "reason": "Not enough candle history for a real cloud yet "
                f"(have {len(df)}, need >= {KIJUN_PERIOD + DISPLACEMENT}).",
                "reasons": [], "conditions": {}, "confidence": 0}

    cloud = cloud_values_at(df, -1)
    cloud_events = detect_cloud_events(df)
    cross = tenkan_kijun_cross(df, cloud)
    chikou = chikou_analysis(df)
    price = df["close"].iloc[-1]
    tenkan, kijun = df["tenkan"].iloc[-1], df["kijun"].iloc[-1]

    ema = _ema(df["close"], ema_period).iloc[-1] if len(df) >= ema_period else None
    vol_sma = df["volume"].rolling(volume_sma_period).mean().iloc[-1] if len(df) >= volume_sma_period else None
    cur_vol = df["volume"].iloc[-1]

    sr = dynamic_support_resistance(df, price, atr)

    bull_conditions = {
        "price_above_cloud": cloud_events["price_vs_cloud"] == "Above Cloud",
        "tenkan_above_kijun": bool(tenkan > kijun) if not (pd.isna(tenkan) or pd.isna(kijun)) else False,
        "future_cloud_green": cloud_events["future_cloud_direction"] == "Bullish",
        "chikou_above_price": chikou["chikou_vs_price"] == "Above Price",
        "cloud_thickness_ok": bool(cloud.thickness and atr and cloud.thickness > atr * CLOUD_THIN_ATR_MULT) if atr else True,
        "adx_pass": bool(adx is not None and adx >= 20),
        "ema_pass": bool(ema is not None and price > ema),
        "volume_pass": bool(vol_sma and cur_vol > vol_sma) if vol_sma else True,
        "vwap_pass": bool(vwap is not None and price > vwap),
        "no_major_resistance_nearby": not (
            sr["nearest_resistance"] is not None and atr and
            sr["distance_from_resistance"] is not None and sr["distance_from_resistance"] <= atr * resistance_proximity_atr_mult
        ),
    }
    bear_conditions = {
        "price_below_cloud": cloud_events["price_vs_cloud"] == "Below Cloud",
        "tenkan_below_kijun": bool(tenkan < kijun) if not (pd.isna(tenkan) or pd.isna(kijun)) else False,
        "future_cloud_red": cloud_events["future_cloud_direction"] == "Bearish",
        "chikou_below_price": chikou["chikou_vs_price"] == "Below Price",
        "cloud_thickness_ok": bull_conditions["cloud_thickness_ok"],
        "adx_pass": bull_conditions["adx_pass"],
        "ema_pass": bool(ema is not None and price < ema),
        "volume_pass": bull_conditions["volume_pass"],
        "vwap_pass": bool(vwap is not None and price < vwap),
        "no_major_support_nearby": not (
            sr["nearest_support"] is not None and atr and
            sr["distance_from_support"] is not None and sr["distance_from_support"] <= atr * resistance_proximity_atr_mult
        ),
    }

    bull_count = sum(bull_conditions.values())
    bear_count = sum(bear_conditions.values())

    if bull_count >= buy_min_conditions and bull_count > bear_count:
        action = "STRONG BUY" if bull_count >= strong_min_conditions else "BUY"
        conditions, count, total = bull_conditions, bull_count, len(bull_conditions)
    elif bear_count >= buy_min_conditions and bear_count > bull_count:
        action = "STRONG SELL" if bear_count >= strong_min_conditions else "SELL"
        conditions, count, total = bear_conditions, bear_count, len(bear_conditions)
    else:
        action = "WAIT"
        conditions = bull_conditions if bull_count >= bear_count else bear_conditions
        count, total = max(bull_count, bear_count), len(conditions)

    reasons = [name.replace("_", " ") for name, passed in conditions.items() if passed]
    confidence = round(count / total * 100) if total else 0

    return {
        "action": action, "confidence": confidence,
        "conditions": conditions, "conditions_passed": count, "conditions_total": total,
        "reasons": reasons, "reason": " | ".join(reasons) if reasons else "No conditions met.",
        "bull_count": bull_count, "bear_count": bear_count,
        "price": round(price, 2), "cloud": asdict(cloud),
    }


def compute_risk_management(
    df: pd.DataFrame,
    direction: str,
    atr: Optional[float] = None,
    atr_mult: float = 1.5,
    rr_multiples: tuple = (1.0, 2.0, 3.0),
    capital: Optional[float] = None,
    risk_per_trade_pct: float = DEFAULT_RISK_PER_TRADE_PCT,
) -> dict:
    """
    Entry/Stop-Loss/Targets/Trailing-Stop/Position-Size in UNDERLYING
    POINTS (see module docstring). direction: 'BUY'/'STRONG BUY' (long) or
    'SELL'/'STRONG SELL' (short).

    Stop candidates: ATR stop (entry -+ atr*atr_mult), Cloud stop (cloud
    edge on the trade's risk side), Kijun stop (classic Ichimoku practice:
    Kijun as the base-line stop). `initial_stop` = whichever of the three
    is CLOSEST to entry (tightest genuine risk, not the loosest) among
    those that are actually on the correct/risk side of entry; a stop on
    the wrong side (e.g. Kijun above entry for a long) is excluded rather
    than silently used.

    Targets: entry +- risk*rr_multiples (standard R-multiple targets, same
    convention sr_probability_engine.py's compute_staged_underlying_targets
    already uses elsewhere in this codebase).

    Trailing stop: suggested to trail to Kijun as it advances (classic
    Ichimoku trailing-stop practice) -- returned as a RULE (string),
    not a single number, since it moves candle-to-candle.

    Position size: risk_amount = capital * risk_per_trade_pct / 100;
    size = floor(risk_amount / risk_per_unit). Returns None (not a guessed
    number) if `capital` isn't supplied -- account size cannot be
    invented, same "graceful None over fabricated data" rule as
    market_structure.calc_vwap.
    """
    is_long = direction in ("BUY", "STRONG BUY")
    is_short = direction in ("SELL", "STRONG SELL")
    out = {"entry": None, "initial_stop": None, "atr_stop": None, "cloud_stop": None,
           "kijun_stop": None, "targets": [], "risk_reward": None, "trailing_stop_rule": None,
           "position_size": None}
    if df.empty or not (is_long or is_short):
        return out

    entry = df["close"].iloc[-1]
    cloud = cloud_values_at(df, -1)
    kijun = df["kijun"].iloc[-1]
    kijun = None if pd.isna(kijun) else round(float(kijun), 2)

    atr_stop = round(entry - atr * atr_mult, 2) if (is_long and atr) else round(entry + atr * atr_mult, 2) if atr else None
    cloud_stop = cloud.bottom if is_long else cloud.top
    candidates = [s for s in (atr_stop, cloud_stop, kijun) if s is not None]
    if is_long:
        candidates = [s for s in candidates if s < entry]
        initial_stop = max(candidates) if candidates else None
    else:
        candidates = [s for s in candidates if s > entry]
        initial_stop = min(candidates) if candidates else None

    targets = []
    risk_reward = None
    if initial_stop is not None:
        risk = abs(entry - initial_stop)
        targets = [round(entry + m * risk, 2) if is_long else round(entry - m * risk, 2) for m in rr_multiples]
        risk_reward = rr_multiples[-1]   # by construction (R-multiple targets)

    position_size = None
    if capital and initial_stop is not None:
        risk_amount = capital * risk_per_trade_pct / 100
        risk_per_unit = abs(entry - initial_stop)
        if risk_per_unit > 0:
            position_size = int(risk_amount // risk_per_unit)

    out.update({
        "entry": round(entry, 2), "initial_stop": initial_stop, "atr_stop": atr_stop,
        "cloud_stop": cloud_stop, "kijun_stop": kijun, "targets": targets,
        "risk_reward": risk_reward,
        "trailing_stop_rule": "Trail stop to Kijun-sen as it advances in your favor.",
        "position_size": position_size,
    })
    return out


def compute_confidence_score(
    signal: dict, cloud_events: dict, momentum: dict, cross: dict,
    adx: Optional[float] = None, htf_agreement_pct: Optional[float] = None,
) -> dict:
    """
    0-100 confidence, built from the SAME sub-factors the spec asks for
    (trend, cloud, momentum, ADX, higher-timeframe agreement, reward:risk)
    with each contribution returned by name (`factors`) -- no black-box
    score, same convention as oi_engine.compute_new_trend_meter.
    """
    factors = {}
    factors["checklist_agreement"] = signal.get("confidence", 0) * 0.4
    factors["cloud_breakout_or_breakdown"] = 10 if (cloud_events.get("cloud_breakout") or cloud_events.get("cloud_breakdown")) else 0
    factors["cloud_rejection_confirms"] = 10 if cloud_events.get("cloud_rejection") else 0
    factors["momentum_accelerating"] = 10 if momentum.get("trend_acceleration") and not momentum.get("possible_reversal") else 0
    factors["exhaustion_penalty"] = -15 if momentum.get("possible_reversal") else 0
    factors["cross_strength"] = min(15.0, cross.get("cross_strength", 0) * 0.15)
    factors["adx_trending_bonus"] = 10 if (adx is not None and adx >= 25) else 0
    factors["htf_agreement_bonus"] = round((htf_agreement_pct or 0) * 0.15, 1)

    score = max(0, min(100, round(sum(factors.values()))))
    return {"score": score, "factors": {k: round(v, 1) for k, v in factors.items()}}


def mtf_confirmation(direction_by_timeframe: dict) -> dict:
    """
    Multi-timeframe confirmation: compares directional lean (any of
    'BUY'/'STRONG BUY' -> bullish, 'SELL'/'STRONG SELL' -> bearish,
    everything else -> neutral) supplied per timeframe by the caller (this
    engine does not fetch other timeframes itself -- callers already own
    candle-fetching for their own timeframe set, e.g. app.py/backtest.py).

    Returns 'Aligned' (all non-neutral timeframes agree), 'Mixed' (neither
    fully aligned nor fully opposite), or 'Opposite' (bullish and bearish
    timeframes both present with neither a clear majority), plus the
    overall majority direction and an agreement percentage used by
    compute_confidence_score's htf_agreement_pct.
    """
    def lean(action):
        if action in ("BUY", "STRONG BUY"):
            return "bullish"
        if action in ("SELL", "STRONG SELL"):
            return "bearish"
        return "neutral"

    leans = {tf: lean(action) for tf, action in (direction_by_timeframe or {}).items()}
    non_neutral = [v for v in leans.values() if v != "neutral"]
    if not non_neutral:
        return {"status": "Mixed", "overall_direction": "Neutral", "agreement_pct": 0.0, "by_timeframe": leans}

    bulls, bears = non_neutral.count("bullish"), non_neutral.count("bearish")
    total = len(non_neutral)
    agreement_pct = round(max(bulls, bears) / total * 100, 1)
    overall = "Bullish" if bulls > bears else "Bearish" if bears > bulls else "Neutral"

    if bulls and bears:
        status = "Opposite" if abs(bulls - bears) <= 1 else "Mixed"
    else:
        status = "Aligned"
    return {"status": status, "overall_direction": overall, "agreement_pct": agreement_pct, "by_timeframe": leans}


# ----------------------------------------------------------------------------
# Top-level orchestrator -- the public API most callers should use
# ----------------------------------------------------------------------------

def analyze(
    candles,
    tenkan_period: int = TENKAN_PERIOD,
    kijun_period: int = KIJUN_PERIOD,
    senkou_b_period: int = SENKOU_B_PERIOD,
    displacement: int = DISPLACEMENT,
    ema_period: int = 50,
    today: Optional[dt.date] = None,
    capital: Optional[float] = None,
    risk_per_trade_pct: float = DEFAULT_RISK_PER_TRADE_PCT,
    htf_directions: Optional[dict] = None,
) -> dict:
    """
    One-shot: candles in, fully structured Ichimoku decision-support dict
    out. Reuses market_structure.calc_adx/calc_atr/calc_vwap on the SAME
    candles (no duplicate ADX/ATR/VWAP math). This is the function
    oi_engine.compute_new_trend_meter / backtest.py / app.py should call.

    `candles`: list[dict] ({'datetime','open','high','low','close','volume'})
    or a pandas DataFrame with the same columns, oldest-first -- see
    _candles_to_df. `today`: needed for VWAP's session boundary, defaults
    to the last candle's date.

    Returns None fields gracefully (not exceptions) wherever there isn't
    enough candle history yet -- same convention as
    market_structure.build_market_structure.
    """
    df = calculate_ichimoku(candles, tenkan_period, kijun_period, senkou_b_period, displacement)
    empty_result = {
        "ichimoku_values": asdict(IchimokuValues(None, None, None, None, None)),
        "cloud": asdict(CloudValues(None, None, None, "Neutral")),
        "trend": "Neutral", "trend_score": 0,
        "support": None, "resistance": None, "momentum": {},
        "entry_signal": "NO_TRADE", "exit_signal": None, "confidence_score": 0,
        "stop_loss": None, "targets": [], "reasons": ["Not enough candle history."],
        "ai_explanation": "Not enough candle history for a real Ichimoku cloud yet.",
        "candle_count": len(df),
    }
    if df.empty or len(df) < kijun_period + displacement:
        return empty_result

    # candle dict list for calc_adx/calc_atr/calc_vwap (market_structure.py's
    # own expected shape) -- built once, reused for all three, not per-call.
    candle_dicts = df.to_dict("records")
    today = today or (df["datetime"].iloc[-1].date() if "datetime" in df.columns else dt.date.today())
    atr = calc_atr(candle_dicts, period=14)
    _, _, adx = calc_adx(candle_dicts, period=14)
    vwap = calc_vwap(candle_dicts, today) if "datetime" in df.columns else None

    row = df.iloc[-1]
    ichimoku_values = IchimokuValues(
        tenkan=None if pd.isna(row["tenkan"]) else round(float(row["tenkan"]), 2),
        kijun=None if pd.isna(row["kijun"]) else round(float(row["kijun"]), 2),
        senkou_a=None if pd.isna(row["senkou_a"]) else round(float(row["senkou_a"]), 2),
        senkou_b=None if pd.isna(row["senkou_b"]) else round(float(row["senkou_b"]), 2),
        chikou=None if pd.isna(row["chikou"]) else round(float(row["chikou"]), 2),
    )
    cloud = cloud_values_at(df, -1)
    cloud_events = detect_cloud_events(df)
    cross = tenkan_kijun_cross(df, cloud)
    chikou = chikou_analysis(df, displacement)
    momentum = momentum_analysis(df, atr=atr)
    trend = classify_trend(cloud_events, cross, chikou, momentum)
    trend_stage = classify_trend_stage(cloud_events, cross, momentum, adx)
    sr = dynamic_support_resistance(df, row["close"], atr)

    signal = generate_ichimoku_signal(df, adx=adx, regime=None, ema_period=ema_period, vwap=vwap, atr=atr)
    htf = mtf_confirmation(htf_directions) if htf_directions else None
    confidence = compute_confidence_score(
        signal, cloud_events, momentum, cross, adx=adx,
        htf_agreement_pct=htf["agreement_pct"] if htf else None,
    )

    risk = {}
    if signal["action"] not in ("NO_TRADE", "WAIT"):
        risk = compute_risk_management(df, signal["action"], atr=atr, capital=capital, risk_per_trade_pct=risk_per_trade_pct)

    reasons = list(signal.get("reasons", []))
    if cloud_events.get("cloud_breakout"):
        reasons.append("cloud breakout")
    if cloud_events.get("cloud_breakdown"):
        reasons.append("cloud breakdown")
    if cloud_events.get("cloud_twist"):
        reasons.append("cloud twist ahead -- trend may flip")
    if momentum.get("possible_reversal"):
        reasons.append("price extended from Kijun -- possible reversal risk")
    if htf:
        reasons.append(f"multi-timeframe {htf['status'].lower()} ({htf['agreement_pct']}% agree)")

    ai_explanation = (
        f"{signal['action']} -- {trend}, price {cloud_events['price_vs_cloud'].lower()}, "
        f"confidence {confidence['score']}%. " + ("; ".join(reasons) if reasons else "No supporting reasons.")
    )

    return {
        "ichimoku_values": asdict(ichimoku_values),
        "cloud": asdict(cloud),
        "cloud_events": cloud_events,
        "cross": cross,
        "chikou": chikou,
        "trend": trend,
        "trend_stage": trend_stage["stage"], "recommended_action": trend_stage["recommended_action"],
        "trend_stage_detail": trend_stage,
        "support": sr["nearest_support"], "resistance": sr["nearest_resistance"],
        "support_resistance": sr,
        "momentum": momentum,
        "entry_signal": signal["action"], "exit_signal": None,
        "signal_detail": signal,
        "confidence_score": confidence["score"], "confidence_factors": confidence["factors"],
        "stop_loss": risk.get("initial_stop"), "targets": risk.get("targets", []),
        "risk_management": risk,
        "reasons": reasons, "ai_explanation": ai_explanation,
        "adx": adx, "atr": atr, "vwap": vwap,
        "mtf": htf,
        "candle_count": len(df),
    }


# ----------------------------------------------------------------------------
# Incremental streaming engine (live mode -- thousands of symbols)
# ----------------------------------------------------------------------------

class IchimokuLiveEngine:
    """
    Per-symbol incremental state for live streaming. Rather than
    recomputing indicators over a symbol's ENTIRE candle history on every
    new tick (what naively re-calling calculate_ichimoku(full_history)
    every cycle would do), this keeps a BOUNDED ring buffer sized exactly
    to what the indicators need (senkou_b_period + displacement + a small
    safety margin -- ~90 candles at textbook defaults) and only ever
    recomputes over that fixed-size window. push() is O(1) amortized
    (deque with maxlen auto-evicts the oldest candle); snapshot() runs the
    vectorized calc + analysis over the bounded window only, independent
    of how many years of history the symbol actually has -- memory and
    per-tick cost are both O(window), not O(total history), so this scales
    to thousands of concurrently-tracked symbols.
    """

    def __init__(self, tenkan_period: int = TENKAN_PERIOD, kijun_period: int = KIJUN_PERIOD,
                 senkou_b_period: int = SENKOU_B_PERIOD, displacement: int = DISPLACEMENT,
                 window_margin: int = 10):
        self.tenkan_period = tenkan_period
        self.kijun_period = kijun_period
        self.senkou_b_period = senkou_b_period
        self.displacement = displacement
        window = max(senkou_b_period, kijun_period, tenkan_period) + displacement + window_margin
        self._candles: deque = deque(maxlen=window)

    def seed(self, candles) -> None:
        """Bulk-load prior history (e.g. from a broker's historical-candles
        fetch) once at startup -- only the tail up to the window size is
        kept, older candles are dropped (they don't affect any current
        indicator value)."""
        df = _candles_to_df(candles)
        for record in df.to_dict("records"):
            self._candles.append(record)

    def push(self, candle: dict) -> None:
        """Append one new (closed) candle. O(1) amortized."""
        self._candles.append(dict(candle))

    def update_forming_candle(self, candle: dict) -> None:
        """Replace the still-forming last candle with its latest tick state
        (high/low/close so far) WITHOUT growing the buffer -- for a live
        LTP-driven intrabar snapshot that doesn't repaint history."""
        if self._candles:
            self._candles[-1] = dict(candle)
        else:
            self._candles.append(dict(candle))

    def __len__(self) -> int:
        return len(self._candles)

    def snapshot(self, **analyze_kwargs) -> dict:
        """Runs analyze() over the current bounded window. Cheap: window is
        fixed-size (~90 candles at defaults), never the symbol's full
        history."""
        return analyze(
            list(self._candles),
            tenkan_period=self.tenkan_period, kijun_period=self.kijun_period,
            senkou_b_period=self.senkou_b_period, displacement=self.displacement,
            **analyze_kwargs,
        )
