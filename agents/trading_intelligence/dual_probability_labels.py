"""
agents/trading_intelligence/dual_probability_labels.py -- walk-forward
event labeling for the dual-probability shadow model (TARGET_EVENT /
STOP_SAFETY_EVENT). Reuses agents.quant_researcher.data_access.load_candles
for real OHLC bars and adapts agents.quant_researcher.strategy_runner's
walk-forward loop (next-bar-open fill convention, same-bar-tie-to-STOP
rule) rather than reimplementing a second barrier-race simulator.

TWO INDEPENDENT EVENTS per candidate entry (direction, entry index,
target distance, stop distance, horizon), NOT complements of each other:

  TARGET_EVENT      = target level touched BEFORE the stop level, within
                       horizon (a first-passage race; same-bar tie -> the
                       race is lost, matching strategy_runner.run_strategy()'s
                       own convention -- never assume the friendlier fill
                       order). None (PENDING) if neither level is touched
                       before the horizon runs out.

  STOP_SAFETY_EVENT = the stop level is NEVER touched at ANY point across
                       the FULL horizon -- tracked via max adverse
                       excursion (mae), which keeps accumulating for the
                       full horizon even after TARGET_EVENT's race has
                       already been decided. A trade can win TARGET_EVENT
                       and still later whip back and breach the stop
                       distance later in the same horizon: TARGET_EVENT
                       and STOP_SAFETY_EVENT can genuinely diverge. This
                       is deliberately NOT `not target_event` -- see
                       test_dual_probability_labels.py's
                       TestIndependence class for a constructed case
                       where they differ.

No lookahead: this module does not generate entry SIGNALS itself (that
is the caller's job, from a real-time feature score at bar i's close,
same convention as strategy_runner.py's _entry_signal -- filled at bar
i+1's open). Given a candidate entry_idx, every field of the returned
label is derived only from bars AT OR AFTER entry_idx, never earlier
ones, and never from data past the end of the loaded archive.
"""
import dataclasses

import pandas as pd


@dataclasses.dataclass
class EntryLabel:
    entry_idx: int
    entry_time: object
    direction: str  # "long" or "short"
    entry_price: float
    target_price: float
    stop_price: float
    horizon_bars: int
    bars_observed: int
    truncated: bool  # True if the archive ran out before horizon_bars elapsed --
                      # both events are UNTRUSTWORTHY when True (the stop/target
                      # could have been touched just past the archive's edge) and
                      # must be excluded from calibration, never counted.
    target_event: bool | None  # None = PENDING (neither level touched within horizon)
    target_event_time: object | None
    stop_safety_event: bool  # True iff the stop level was never touched, full horizon
    mfe: float
    mae: float


def label_entry(candles: pd.DataFrame, entry_idx: int, *, direction: str,
                 target_distance: float, stop_distance: float, horizon_bars: int) -> EntryLabel | None:
    """Labels ONE candidate entry. `candles` must be datetime-sorted with
    open/high/low/close/datetime columns (matches
    backtest.load_intraday_candles's shape, i.e.
    agents.quant_researcher.data_access.load_candles's return shape).
    `entry_idx` is the fill bar (already the bar AFTER the signal, per
    strategy_runner.py's convention -- this function does not itself add
    the +1). Returns None if there is no bar left to fill at entry_idx
    (can't label an entry with no price to enter at)."""
    if candles is None or candles.empty or entry_idx >= len(candles):
        return None
    if target_distance <= 0 or stop_distance <= 0 or horizon_bars <= 0:
        raise ValueError("target_distance, stop_distance, and horizon_bars must all be positive")

    n = len(candles)
    entry_price = float(candles["open"].iloc[entry_idx])
    entry_time = candles["datetime"].iloc[entry_idx]
    target_price = entry_price + target_distance if direction == "long" else entry_price - target_distance
    stop_price = entry_price - stop_distance if direction == "long" else entry_price + stop_distance

    last_idx = min(entry_idx + horizon_bars, n - 1)
    bars_observed = last_idx - entry_idx + 1
    truncated = bars_observed < horizon_bars + 1  # entry bar itself + horizon_bars forward bars

    target_event = None
    target_event_time = None
    race_decided = False
    mfe, mae = 0.0, 0.0
    stop_touched_anywhere = False

    for j in range(entry_idx, last_idx + 1):
        bar = candles.iloc[j]
        excursion = (bar["high"] - entry_price) if direction == "long" else (entry_price - bar["low"])
        adverse = (entry_price - bar["low"]) if direction == "long" else (bar["high"] - entry_price)
        mfe = max(mfe, excursion)
        mae = max(mae, adverse)

        hit_target = bar["high"] >= target_price if direction == "long" else bar["low"] <= target_price
        hit_stop = bar["low"] <= stop_price if direction == "long" else bar["high"] >= stop_price
        if hit_stop:
            stop_touched_anywhere = True

        if not race_decided and (hit_target or hit_stop):
            # Same-bar tie (both levels inside one bar's range) resolves
            # to the adverse side -- never assume the friendlier fill
            # order, matching strategy_runner.run_strategy()'s rule.
            target_event = bool(hit_target and not hit_stop)
            target_event_time = bar["datetime"]
            race_decided = True
        # mfe/mae/stop_touched_anywhere keep accumulating for the FULL
        # horizon even after race_decided -- this is what makes
        # STOP_SAFETY_EVENT genuinely independent of TARGET_EVENT.

    return EntryLabel(
        entry_idx=entry_idx, entry_time=entry_time, direction=direction,
        entry_price=entry_price, target_price=target_price, stop_price=stop_price,
        horizon_bars=horizon_bars, bars_observed=bars_observed, truncated=truncated,
        target_event=target_event, target_event_time=target_event_time,
        stop_safety_event=not stop_touched_anywhere,
        mfe=round(float(mfe), 4), mae=round(float(mae), 4),
    )
