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

Two groups are HONEST GAPS, not fabricated, per the Phase 0 diagnosis:
  VOLUME -- confirmed absent anywhere in this repo (no OBV/MFI/
            volume-price-confirmation function exists). Always None.
  MTF    -- FEATURE_REGISTRY operates on ONE candle series; a genuine
            multi-timeframe agreement score would need
            agents.trading_intelligence.multi_timeframe.synchronize()
            bridged in, a real integration task not done in this first
            pass. Always None until built.

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


@dataclasses.dataclass
class FeatureGroups:
    trend: float | None
    momentum: float | None
    structure: float | None
    oi: float | None
    volume: float | None   # always None -- see module docstring
    mtf: float | None      # always None -- see module docstring
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

    return FeatureGroups(
        trend=trend, momentum=momentum, structure=structure, oi=oi,
        volume=None, mtf=None, regime=regime,
    )
