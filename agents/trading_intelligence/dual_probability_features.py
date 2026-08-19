"""
agents/trading_intelligence/dual_probability_features.py -- maps this
repo's ALREADY-EXISTING, point-in-time-safe feature functions
(agents.quant_researcher.features.FEATURE_REGISTRY for candle/OI-derived
signals, market_structure.calc_adx/classify_regime for regime) into the
7 evidence groups the dual-probability model scores on: TREND, MOMENTUM,
STRUCTURE, OI, VOLUME, MTF, REGIME.

ANTI-DOUBLE-COUNTING (per the approved design): each group collapses to
exactly ONE scalar value at a given bar index, never a sum of several
raw indicators from features.py -- summing multiple correlated
sub-signals (e.g. atr + range_compression, both volatility-flavored)
would silently inflate one underlying signal into "more evidence" than
it really is. Where a group has more than one candidate feature in
FEATURE_REGISTRY, only the single feature this module designates as
that group's representative is used; expanding a group to a real
multi-feature composite (with correlation control) is tracked as future
work, not silently done via naive summation.

VOLUME and MTF, added after the Phase 0 diagnosis flagged them as
gaps, are built here directly (no reusable function existed for either
in this repo):

  VOLUME -- Money Flow Index (a standard, bounded volume-weighted RSI
            analogue), computed directly from the candle archive's own
            `volume` column. CONFIRMED real and populated for the MCX
            commodity symbols this session's investigation has focused
            on (CRUDEOIL/CRUDEOILM/NATURALGAS/NATGASMINI/GOLD -- 100%
            non-zero rows, checked directly), but genuinely always zero
            for NSE index symbols (NIFTY/BANKNIFTY -- indices aren't
            directly traded instruments, so there is no real volume to
            report). `_mfi_at()` detects an all-zero volume window and
            returns None rather than computing a degenerate MFI from
            fabricated-looking data.

  MTF    -- a real point-in-time-safe multi-timeframe agreement score:
            resamples the SAME already-loaded 3m candles (bounded to
            bars up to and including the query index -- never a live,
            unbounded pull via multi_timeframe.get_timeframe(), which
            would violate no-lookahead if used naively in backtest
            replay) to 15-minute bars using the identical OHLCV
            aggregation agents.trading_intelligence.multi_timeframe.py
            already established (open=first/high=max/low=min/close=last),
            then compares the native (3m) short-term direction against
            the resampled (15m) short-term direction. +1 = both agree
            bullish, -1 = both agree bearish, 0 = disagree or flat.
            None when there isn't enough history yet for a meaningful
            15m read -- never fabricated.

Missing/stale data is NEUTRAL: every field is Optional and this module
never substitutes a default value for a group it couldn't compute --
callers (the calibration layer) must treat None as "this group did not
vote," never as bullish or bearish evidence, and must count how many
non-None groups an observation actually has (group_count()) rather than
assuming all 7 are always present.
"""
import dataclasses

import pandas as pd

from agents.quant_researcher import features as qr_features

MFI_WINDOW = 14
MTF_RESAMPLE_RULE = "15min"
MTF_NATIVE_LOOKBACK_BARS = 5
MTF_HIGHER_LOOKBACK_BARS = 3
MTF_MIN_HISTORY_BARS = 60  # ~4 hours of 3m bars -- enough for a few real 15m bars to exist


@dataclasses.dataclass
class FeatureGroups:
    trend: float | None
    momentum: float | None
    structure: float | None
    oi: float | None
    volume: float | None   # MFI-based, None where volume data is absent/zero -- see module docstring
    mtf: float | None      # 15m-vs-3m agreement, None below MTF_MIN_HISTORY_BARS -- see module docstring
    regime: str | None     # categorical (TRENDING/RANGING/TRANSITIONING/UNKNOWN), not a scalar

    def group_count(self) -> int:
        """Number of non-None evidence groups actually available for this
        observation. Used for the "minimum independent evidence groups
        confirmed" gate -- never assume all 7 are present."""
        scalar_groups = (self.trend, self.momentum, self.structure, self.oi, self.volume, self.mtf)
        return sum(1 for v in scalar_groups if v is not None) + (1 if self.regime not in (None, "UNKNOWN") else 0)


def _value_at(series: pd.Series | None, idx: int) -> float | None:
    if series is None or idx < 0 or idx >= len(series):
        return None
    val = series.iloc[idx]
    if pd.isna(val):
        return None
    return float(val)


def _regime_at(candles: pd.DataFrame, idx: int) -> str | None:
    """Self-contained regime read for backtest/calibration use: computes
    ADX from candles up to and including `idx` only (never later bars --
    point-in-time safe by construction, matching calc_adx's own oldest-
    first list convention). Live callers (e.g. a future signal_graph.py
    shadow node) already have a regime computed earlier in the same
    cycle and should pass it straight through instead of calling this,
    to avoid a second, possibly-inconsistent regime read -- this
    fallback exists purely for standalone backtest replay."""
    import market_structure
    if idx < 0 or idx >= len(candles):
        return None
    window = candles.iloc[max(0, idx - 200): idx + 1]
    records = window[["high", "low", "close"]].to_dict("records")
    _plus_di, _minus_di, adx = market_structure.calc_adx(records)
    return market_structure.classify_regime(adx)


def _mfi_at(candles: pd.DataFrame, idx: int, *, window: int = MFI_WINDOW) -> float | None:
    """Money Flow Index at bar `idx`, using only bars up to and including
    idx (point-in-time safe by construction -- the rolling window is
    computed on a bounded slice, never the full series). Returns
    (MFI-50)/50, centered at 0 and bounded [-1, 1] like the other
    centered groups (trend, oi). None if there's insufficient history,
    or if the window's volume is entirely zero/missing (NSE index
    symbols have no real traded volume -- reporting a degenerate MFI
    from all-zero volume would be fabricating a value, not computing
    one)."""
    if idx < window or "volume" not in candles.columns:
        return None
    win = candles.iloc[idx - window: idx + 1]
    if win["volume"].fillna(0).abs().sum() == 0:
        return None  # no real volume data for this instrument -- honest None, not a fabricated MFI

    typical_price = (win["high"] + win["low"] + win["close"]) / 3.0
    raw_flow = typical_price * win["volume"]
    price_diff = typical_price.diff()
    positive_flow = raw_flow.where(price_diff > 0, 0.0).iloc[1:].sum()
    negative_flow = raw_flow.where(price_diff < 0, 0.0).iloc[1:].sum()
    if negative_flow == 0:
        mfi = 100.0 if positive_flow > 0 else 50.0
    else:
        money_ratio = positive_flow / negative_flow
        mfi = 100.0 - (100.0 / (1.0 + money_ratio))
    return (float(mfi) - 50.0) / 50.0


def _mtf_agreement_at(candles: pd.DataFrame, idx: int) -> float | None:
    """Multi-timeframe agreement at bar `idx`: compares the native 3m
    short-term direction against a 15m-resampled short-term direction,
    both computed ONLY from bars up to and including idx (bounded slice
    -- never a live/unbounded pull, so no lookahead is possible even
    though the resampled bucket containing idx may still be "forming"
    from a live perspective; it is built from exclusively real, already-
    closed 3m bars). +1 = both directions agree bullish, -1 = both agree
    bearish, 0 = they disagree or either is flat. None below
    MTF_MIN_HISTORY_BARS or if the 15m resample doesn't yet have enough
    bars for a short-term read -- never fabricated."""
    if idx < MTF_MIN_HISTORY_BARS:
        return None
    window = candles.iloc[max(0, idx - 300): idx + 1]

    native_recent = window["close"].iloc[-MTF_NATIVE_LOOKBACK_BARS:]
    if len(native_recent) < 2:
        return None
    native_direction = 0.0
    if native_recent.iloc[-1] != native_recent.iloc[0]:
        native_direction = 1.0 if native_recent.iloc[-1] > native_recent.iloc[0] else -1.0

    resampled = (
        window.set_index("datetime")
        .resample(MTF_RESAMPLE_RULE)
        .agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        .dropna(subset=["open"])
    )
    if len(resampled) < MTF_HIGHER_LOOKBACK_BARS + 1:
        return None
    higher_recent = resampled["close"].iloc[-MTF_HIGHER_LOOKBACK_BARS:]
    higher_direction = 0.0
    if higher_recent.iloc[-1] != higher_recent.iloc[0]:
        higher_direction = 1.0 if higher_recent.iloc[-1] > higher_recent.iloc[0] else -1.0

    return native_direction * higher_direction


def extract_feature_groups(ctx: qr_features.FeatureContext, idx: int, *,
                            regime_override: str | None = None) -> FeatureGroups:
    """Extracts the 7 evidence groups' scalar values AT bar `idx` from
    already-computed, point-in-time-safe series (ctx.candles/ctx.cycles
    -- see FEATURE_REGISTRY's own no-lookahead discipline, enforced by
    its merge_asof(direction="backward") join). `idx` must be a bar that
    has ALREADY closed relative to the prediction time; callers are
    responsible for that -- this function only indexes into series that
    are themselves already causal.

    `regime_override`: pass the caller's already-computed regime (e.g.
    from a live cycle) to skip the self-contained ADX recompute in
    _regime_at(). Left None for standalone backtest replay."""
    trend = _value_at(qr_features.vwap_deviation(ctx), idx)
    momentum = _value_at(qr_features.momentum_exhaustion(ctx), idx)
    structure = _value_at(qr_features.range_compression(ctx), idx)

    oi = None
    if ctx.cycles:
        oi = _value_at(qr_features.oi_delta_bias(ctx), idx)

    regime = regime_override if regime_override is not None else _regime_at(ctx.candles, idx)
    volume = _mfi_at(ctx.candles, idx)
    mtf = _mtf_agreement_at(ctx.candles, idx)

    return FeatureGroups(
        trend=trend, momentum=momentum, structure=structure, oi=oi,
        volume=volume, mtf=mtf, regime=regime,
    )
