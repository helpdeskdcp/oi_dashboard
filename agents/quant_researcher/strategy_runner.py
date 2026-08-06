"""
agents/quant_researcher/strategy_runner.py -- the ONE generic interpreter
that turns any StrategySpec into simulated trades against OHLCV data.
"No hardcoded strategy logic" means every hypothesis -- OI+Delta,
VWAP+Gamma, momentum exhaustion, all of them -- runs through this exact
same loop; only the StrategySpec's features/thresholds/direction change.

This is a fresh, underlying-price-only trade simulator, NOT a rewrite of
backtest.py's option-strike-level engines (simulate_sr_engine_trades,
simulate_ichimoku_trades, etc, which replay actual option premiums tick
by tick with real fills) -- those exist to validate an already-built,
already-promoted strategy at full fidelity. This one exists purely to
screen research hypotheses cheaply, in underlying-points terms, before
anything is worth that fidelity. The trade-dict shape produced here
(points/exit_reason/mfe/mae) matches exactly what
backtest.compute_advanced_trade_stats already expects from every other
engine in this repo, so agents.quant_researcher.metrics can reuse it
unmodified.

No lookahead: every feature in features.py is causal (rolling windows,
shift-based, or a backward-only as-of join for option-chain data), so
the entry signal at bar i's close is a hard fact by end of bar i. The
fill itself happens one bar later, at bar i+1's open -- "saw the signal
at close, acted at the next open," the same convention a human
systematic trader would use, and one full bar removed from the signal
that produced it.
"""
import pandas as pd

from . import features
from .strategy_spec import StrategySpec


def _feature_scores(spec: StrategySpec, ctx: features.FeatureContext) -> pd.DataFrame:
    invert = spec.params.get("invert", {}) if spec.params else {}
    scores = {}
    for name in spec.features:
        series = features.compute_feature(name, ctx)
        threshold = spec.thresholds.get(name, 0.0)
        scores[name] = (threshold - series) if invert.get(name) else (series - threshold)
    return pd.DataFrame(scores, index=ctx.candles.index)


def _entry_signal(spec: StrategySpec, score_df: pd.DataFrame) -> pd.Series:
    """AND-combinator: every feature's score must agree on direction.
    Long when every score > 0, short when every score < 0 -- the "hybrid
    strategy" mechanism: a two-feature hypothesis (e.g. VWAP + Gamma)
    only fires when BOTH conditions line up, never either alone."""
    if score_df.empty or score_df.shape[1] == 0:
        return pd.Series(0, index=score_df.index, dtype=int)
    valid = score_df.notna().all(axis=1)
    long_signal = (score_df > 0).all(axis=1) & valid
    short_signal = (score_df < 0).all(axis=1) & valid
    if spec.direction == "long":
        short_signal = pd.Series(False, index=score_df.index)
    elif spec.direction == "short":
        long_signal = pd.Series(False, index=score_df.index)
    return long_signal.astype(int) - short_signal.astype(int)  # +1 long, -1 short, 0 none


def run_strategy(spec: StrategySpec, candles: pd.DataFrame, cycles: list | None = None,
                  *, expiry_dates: set | None = None) -> list:
    """Returns a list of trade dicts (points/exit_reason/mfe/mae, plus
    entry_time/exit_time/direction/entry_price/exit_price), one per
    completed trade, chronological, non-overlapping (one position at a
    time). Empty list if candles has too few rows to ever fill an entry."""
    if candles is None or candles.empty or len(candles) < 2:
        return []
    candles = candles.sort_values("datetime").reset_index(drop=True)
    ctx = features.FeatureContext(candles=candles, cycles=cycles, expiry_dates=expiry_dates)
    signal = _entry_signal(spec, _feature_scores(spec, ctx))

    trades = []
    n = len(candles)
    i = 0
    while i < n - 1:
        sig = signal.iloc[i]
        if sig == 0:
            i += 1
            continue

        direction = "long" if sig > 0 else "short"
        entry_idx = i + 1  # filled at the NEXT bar's open -- see module docstring
        entry_price = float(candles["open"].iloc[entry_idx])
        entry_time = candles["datetime"].iloc[entry_idx]

        target = entry_price + spec.target_points if direction == "long" else entry_price - spec.target_points
        stop = entry_price - spec.stop_points if direction == "long" else entry_price + spec.stop_points

        exit_price, exit_reason, exit_time = None, None, None
        mfe, mae = 0.0, 0.0
        last_idx = min(entry_idx + spec.max_hold_bars, n - 1)
        for j in range(entry_idx, last_idx + 1):
            bar = candles.iloc[j]
            excursion = (bar["high"] - entry_price) if direction == "long" else (entry_price - bar["low"])
            adverse = (entry_price - bar["low"]) if direction == "long" else (bar["high"] - entry_price)
            mfe = max(mfe, excursion)
            mae = max(mae, adverse)

            hit_target = bar["high"] >= target if direction == "long" else bar["low"] <= target
            hit_stop = bar["low"] <= stop if direction == "long" else bar["high"] >= stop
            if hit_target and hit_stop:
                # Ambiguous same-bar fill (both levels inside one bar's
                # range) -- conservatively resolve to the adverse side,
                # never assume the friendlier fill order.
                exit_price, exit_reason, exit_time = stop, "STOP LOSS", bar["datetime"]
                break
            if hit_target:
                exit_price, exit_reason, exit_time = target, "TARGET HIT", bar["datetime"]
                break
            if hit_stop:
                exit_price, exit_reason, exit_time = stop, "STOP LOSS", bar["datetime"]
                break
            if j == last_idx:
                exit_price, exit_reason, exit_time = float(bar["close"]), "TIME EXIT", bar["datetime"]

        points = (exit_price - entry_price) if direction == "long" else (entry_price - exit_price)
        trades.append({
            "entry_time": entry_time, "exit_time": exit_time, "direction": direction,
            "entry_price": entry_price, "exit_price": exit_price,
            "points": round(float(points), 2), "exit_reason": exit_reason,
            "mfe": round(float(mfe), 2), "mae": round(float(mae), 2),
        })
        i = last_idx + 1  # no overlapping trades

    return trades
