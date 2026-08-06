"""
agents/quant_researcher/features.py -- FEATURE_REGISTRY: a plug-in
library of named, pure indicator/formula functions. Each one is a
building block, never a strategy -- "Research modules must be plug-in
based. No hardcoded strategy logic" means the entry/exit decision lives
in strategy_runner.py's generic interpreter, not here. Adding a new
research idea to this codebase means adding one function and registering
it; nothing else changes.

Every feature function has the signature `fn(ctx: FeatureContext) ->
pd.Series`, index-aligned to `ctx.candles`'s row index (same length),
so strategy_runner.py can combine any two features positionally without
knowing which are OHLCV-derived and which are option-chain-derived.

Two data sources, both optional at the FeatureContext level so a feature
degrades gracefully (returns an all-NaN series, never raises) when its
required input isn't available -- matching backtest.py's own
load_intraday_candles/load_market_structure_snapshots convention of
"missing data returns something safely inert, not an exception":
  - ctx.candles: OHLCV DataFrame (agents.quant_researcher.data_access.load_candles)
  - ctx.cycles:  normalized option-chain cycles, one dict per cycle, each
                 with a "strikes" list (agents.quant_researcher.data_access.load_cycles_for_range)

Point-in-time alignment: every cycle-derived feature uses a backward
as-of join (pandas.merge_asof, direction="backward") to attach each
candle to the most recent cycle AT OR BEFORE that candle's timestamp --
never a later one. Using a later cycle's data would be lookahead bias,
the same failure mode load_market_structure_snapshots's own docstring
warns about.
"""
import dataclasses

import numpy as np
import pandas as pd


@dataclasses.dataclass
class FeatureContext:
    candles: pd.DataFrame
    cycles: list | None = None
    expiry_dates: set | None = None


def _empty_like(ctx: FeatureContext) -> pd.Series:
    return pd.Series([np.nan] * len(ctx.candles), index=ctx.candles.index, dtype=float)


def _true_range(candles: pd.DataFrame) -> pd.Series:
    prev_close = candles["close"].shift(1)
    return pd.concat(
        [
            candles["high"] - candles["low"],
            (candles["high"] - prev_close).abs(),
            (candles["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def _rsi(close: pd.Series, window: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.rolling(window, min_periods=window).mean()
    avg_loss = loss.rolling(window, min_periods=window).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def _daily_cpr(candles: pd.DataFrame) -> pd.DataFrame:
    """Classic CPR (pivot/BC/TC) computed from each PRIOR day's OHLC,
    returned as one row per day the CPR applies to. Using the prior day
    (not the current, still-forming day) is deliberate -- CPR is always a
    forecast for "today" derived from "yesterday", never computed from
    data that wasn't known yet at the start of the session."""
    daily = candles.set_index("datetime")[["high", "low", "close"]].resample("1D").agg(
        {"high": "max", "low": "min", "close": "last"}
    ).dropna()
    pivot = (daily["high"] + daily["low"] + daily["close"]) / 3
    bc = (daily["high"] + daily["low"]) / 2
    tc = 2 * pivot - bc
    out = pd.DataFrame({"pivot": pivot, "bc": bc, "tc": tc})
    out.index = out.index + pd.Timedelta(days=1)  # applies to the NEXT trading day
    return out


def _align_cycle_values(ctx: FeatureContext, cycle_ts: list, values: list) -> pd.Series:
    if not cycle_ts:
        return _empty_like(ctx)
    cycle_df = pd.DataFrame({"ts": pd.to_datetime(cycle_ts, errors="coerce"), "value": values}).dropna(subset=["ts"])
    if cycle_df.empty:
        return _empty_like(ctx)
    cycle_df = cycle_df.sort_values("ts")
    candles_sorted = ctx.candles[["datetime"]].reset_index().sort_values("datetime")
    merged = pd.merge_asof(candles_sorted, cycle_df, left_on="datetime", right_on="ts", direction="backward")
    aligned = pd.Series(merged["value"].values, index=merged["index"].values)
    return aligned.reindex(ctx.candles.index)


def _cycle_strike_sum(cycle: dict, field_a: str, field_b: str, *, near_atm_only: bool = False) -> float:
    atm = cycle.get("atm")
    total = 0.0
    for s in cycle.get("strikes") or []:
        if near_atm_only and atm is not None and s.get("strike") is not None:
            if abs(s["strike"] - atm) > (2 * _strike_step_guess(cycle)):
                continue
        total += (s.get(field_a) or 0.0)
        if field_b:
            total += (s.get(field_b) or 0.0)
    return total


def _strike_step_guess(cycle: dict) -> float:
    strikes = sorted({s["strike"] for s in (cycle.get("strikes") or []) if s.get("strike") is not None})
    if len(strikes) < 2:
        return 50.0
    return min(b - a for a, b in zip(strikes, strikes[1:])) or 50.0


# --- OHLCV-only primitives (always computable) -----------------------------

def atr(ctx: FeatureContext, *, window: int = 14) -> pd.Series:
    if ctx.candles.empty:
        return _empty_like(ctx)
    return _true_range(ctx.candles).rolling(window, min_periods=1).mean()


def vwap_deviation(ctx: FeatureContext, *, window: int = 20) -> pd.Series:
    if ctx.candles.empty:
        return _empty_like(ctx)
    typical = (ctx.candles["high"] + ctx.candles["low"] + ctx.candles["close"]) / 3
    if "volume" in ctx.candles.columns and ctx.candles["volume"].fillna(0).sum() > 0:
        vol = ctx.candles["volume"].fillna(0.0)
        vwap = (typical * vol).rolling(window, min_periods=1).sum() / vol.rolling(window, min_periods=1).sum().replace(0.0, np.nan)
    else:
        vwap = typical.rolling(window, min_periods=1).mean()
    return ((ctx.candles["close"] - vwap) / vwap.replace(0.0, np.nan)).fillna(0.0)


def range_compression(ctx: FeatureContext, *, short: int = 5, long: int = 20) -> pd.Series:
    """Short-window average bar range vs long-window average bar range.
    Low values -> the market has been compressing -> feeds
    range_breakout_probability (1 - this, clipped) in hypotheses.py."""
    if ctx.candles.empty:
        return _empty_like(ctx)
    bar_range = ctx.candles["high"] - ctx.candles["low"]
    short_avg = bar_range.rolling(short, min_periods=1).mean()
    long_avg = bar_range.rolling(long, min_periods=1).mean().replace(0.0, np.nan)
    return (short_avg / long_avg).fillna(1.0)


def momentum_exhaustion(ctx: FeatureContext, *, window: int = 14) -> pd.Series:
    """RSI at a >70/<30 extreme, weighted by how fast RSI is now
    weakening (negative rate-of-change at an overbought extreme, or
    positive rate-of-change at an oversold extreme signals the move is
    running out of steam) -- 0 everywhere RSI isn't at an extreme."""
    if ctx.candles.empty:
        return _empty_like(ctx)
    rsi = _rsi(ctx.candles["close"], window)
    rsi_roc = rsi.diff().fillna(0.0)
    overbought = (rsi > 70) & (rsi_roc < 0)
    oversold = (rsi < 30) & (rsi_roc > 0)
    score = pd.Series(0.0, index=ctx.candles.index)
    score[overbought] = -(rsi[overbought] - 70) / 30.0
    score[oversold] = (30 - rsi[oversold]) / 30.0
    return score


def liquidity_sweep(ctx: FeatureContext, *, lookback: int = 20) -> pd.Series:
    """A wick-based stop-hunt detector: this bar's low pierces the prior
    `lookback` bars' minimum low but closes back above it (positive
    score -- bullish sweep of stops below the range), or the mirror image
    on the high side (negative score). 0 otherwise. A generic,
    OHLCV-only reimplementation for backtesting arbitrary symbols/ranges
    -- distinct from (and not a rewrite of) market_structure.py's live
    per-day liquidity_sweep detection, which only exists where a snapshot
    was actually saved."""
    if ctx.candles.empty:
        return _empty_like(ctx)
    prior_low = ctx.candles["low"].shift(1).rolling(lookback, min_periods=1).min()
    prior_high = ctx.candles["high"].shift(1).rolling(lookback, min_periods=1).max()
    bullish = (ctx.candles["low"] < prior_low) & (ctx.candles["close"] > prior_low)
    bearish = (ctx.candles["high"] > prior_high) & (ctx.candles["close"] < prior_high)
    score = pd.Series(0.0, index=ctx.candles.index)
    score[bullish] = (prior_low[bullish] - ctx.candles.loc[bullish, "low"]).abs()
    score[bearish] = -(ctx.candles.loc[bearish, "high"] - prior_high[bearish]).abs()
    return score


def cpr_width(ctx: FeatureContext) -> pd.Series:
    if ctx.candles.empty:
        return _empty_like(ctx)
    cpr = _daily_cpr(ctx.candles)
    if cpr.empty:
        return _empty_like(ctx)
    dates = ctx.candles["datetime"].dt.normalize()
    width = (cpr["tc"] - cpr["bc"]).abs() / cpr["pivot"].replace(0.0, np.nan)
    return dates.map(width).fillna(0.0).reset_index(drop=True).set_axis(ctx.candles.index)


def cpr_position(ctx: FeatureContext) -> pd.Series:
    if ctx.candles.empty:
        return _empty_like(ctx)
    cpr = _daily_cpr(ctx.candles)
    if cpr.empty:
        return _empty_like(ctx)
    dates = ctx.candles["datetime"].dt.normalize()
    pivot = dates.map(cpr["pivot"])
    band = dates.map((cpr["tc"] - cpr["bc"]).abs()).replace(0.0, np.nan)
    position = (ctx.candles["close"] - pivot) / band
    return position.fillna(0.0).reset_index(drop=True).set_axis(ctx.candles.index)


def expiry_flag(ctx: FeatureContext) -> pd.Series:
    if ctx.candles.empty:
        return _empty_like(ctx)
    if not ctx.expiry_dates:
        return pd.Series(0.0, index=ctx.candles.index)
    dates = ctx.candles["datetime"].dt.strftime("%Y-%m-%d")
    return dates.isin(ctx.expiry_dates).astype(float).reset_index(drop=True).set_axis(ctx.candles.index)


# --- option-chain (cycle) primitives (need ctx.cycles) ----------------------

def oi_delta_bias(ctx: FeatureContext) -> pd.Series:
    """Delta-weighted OI-change bias per cycle: call-side OI buildup
    (weighted by |delta|) minus put-side, i.e. "OI + Delta combinations"."""
    if not ctx.cycles or ctx.candles.empty:
        return _empty_like(ctx)
    ts, values = [], []
    for c in ctx.cycles:
        ce = sum((s.get("ce_oi_chg") or 0.0) * abs(s.get("ce_delta") or 0.0) for s in c.get("strikes") or [])
        pe = sum((s.get("pe_oi_chg") or 0.0) * abs(s.get("pe_delta") or 0.0) for s in c.get("strikes") or [])
        ts.append(c.get("ts"))
        values.append(ce - pe)
    return _align_cycle_values(ctx, ts, values)


def gamma_exposure(ctx: FeatureContext) -> pd.Series:
    if not ctx.cycles or ctx.candles.empty:
        return _empty_like(ctx)
    ts = [c.get("ts") for c in ctx.cycles]
    values = [_cycle_strike_sum(c, "ce_gamma", "pe_gamma", near_atm_only=True) for c in ctx.cycles]
    return _align_cycle_values(ctx, ts, values)


def premium_expansion(ctx: FeatureContext, *, window: int = 5) -> pd.Series:
    """Rate of change of the ATM straddle premium (CE + PE ltp at the
    strike closest to that cycle's ATM) over a short rolling window of
    cycles -- "ATR + Premium Expansion" pairs this with atr()."""
    if not ctx.cycles or ctx.candles.empty:
        return _empty_like(ctx)
    ts, straddle = [], []
    for c in ctx.cycles:
        atm = c.get("atm")
        strikes = c.get("strikes") or []
        row = min(strikes, key=lambda s: abs((s.get("strike") or 0) - atm)) if atm is not None and strikes else None
        straddle.append(((row or {}).get("ce_ltp") or 0.0) + ((row or {}).get("pe_ltp") or 0.0))
        ts.append(c.get("ts"))
    series = pd.Series(straddle)
    roc = series.pct_change(periods=min(window, max(len(series) - 1, 1))).fillna(0.0).tolist()
    return _align_cycle_values(ctx, ts, roc)


def max_pain_distance(ctx: FeatureContext) -> pd.Series:
    if not ctx.cycles or ctx.candles.empty:
        return _empty_like(ctx)
    ts, values = [], []
    for c in ctx.cycles:
        spot, max_pain = c.get("underlying_ltp"), c.get("max_pain")
        if spot and max_pain:
            values.append((spot - max_pain) / spot)
        else:
            values.append(0.0)
        ts.append(c.get("ts"))
    return _align_cycle_values(ctx, ts, values)


def institutional_activity(ctx: FeatureContext) -> pd.Series:
    """Total OI concentrated near the ATM strike -- a proxy for
    institutional positioning weight, meant to pair with cpr_position()
    ("CPR + Institutional Activity")."""
    if not ctx.cycles or ctx.candles.empty:
        return _empty_like(ctx)
    ts = [c.get("ts") for c in ctx.cycles]
    values = [_cycle_strike_sum(c, "ce_oi", "pe_oi", near_atm_only=True) for c in ctx.cycles]
    return _align_cycle_values(ctx, ts, values)


def iv_crush(ctx: FeatureContext) -> pd.Series:
    """Average ATM-ish IV minus its own running session-to-date maximum --
    negative values mean IV has come off its session high (a crush)."""
    if not ctx.cycles or ctx.candles.empty:
        return _empty_like(ctx)
    ts, avg_iv = [], []
    for c in ctx.cycles:
        strikes = c.get("strikes") or []
        ivs = [s.get("ce_iv") or 0.0 for s in strikes] + [s.get("pe_iv") or 0.0 for s in strikes]
        avg_iv.append(sum(ivs) / len(ivs) if ivs else 0.0)
        ts.append(c.get("ts"))
    series = pd.Series(avg_iv)
    running_max = series.cummax().replace(0.0, np.nan)
    crush = (series - running_max).fillna(0.0).tolist()
    return _align_cycle_values(ctx, ts, crush)


FEATURE_REGISTRY = {
    "atr": atr,
    "vwap_deviation": vwap_deviation,
    "range_compression": range_compression,
    "momentum_exhaustion": momentum_exhaustion,
    "liquidity_sweep": liquidity_sweep,
    "cpr_width": cpr_width,
    "cpr_position": cpr_position,
    "expiry_flag": expiry_flag,
    "oi_delta_bias": oi_delta_bias,
    "gamma_exposure": gamma_exposure,
    "premium_expansion": premium_expansion,
    "max_pain_distance": max_pain_distance,
    "institutional_activity": institutional_activity,
    "iv_crush": iv_crush,
}


def compute_feature(name: str, ctx: FeatureContext) -> pd.Series:
    if name not in FEATURE_REGISTRY:
        raise KeyError(f"unknown feature {name!r} -- not in FEATURE_REGISTRY")
    return FEATURE_REGISTRY[name](ctx)
