"""
agents/trading_intelligence/dual_probability_backtest.py -- builds a
real, walk-forward-labeled dataset from actual historical OHLC data
(agents.quant_researcher.data_access.load_candles) and fits+validates
the two-stage calibrated dual-probability model
(dual_probability_calibration.CalibratedProbabilityModel) for both
TARGET_EVENT and STOP_SAFETY_EVENT, per direction.

DATASET CONSTRUCTION: steps through each symbol's candles at a
non-overlapping stride of `horizon_bars` (matching
agents.quant_researcher.strategy_runner.run_strategy()'s own "no
overlapping trades" discipline -- consecutive bars would otherwise share
almost the same future price path, badly inflating the effective sample
size with correlated near-duplicates). At each step, BOTH a long and a
short candidate entry are labeled from the SAME feature-group snapshot
(features themselves are direction-agnostic; direction only affects
which way the target/stop distances point) -- this gives balanced
direction coverage without depending on any specific entry-trigger
threshold, unlike a real trading signal.

Target/stop distances are ATR-scaled (target = 1.5x, stop = 0.75x the
bar's rolling ATR -- a conventional 2:1 reward:risk starting geometry,
not an arbitrary number) via agents.quant_researcher.features.atr(),
matching this module's own no-lookahead discipline.

WALK-FORWARD ONLY: dataset rows are chronological; the split into
train/calibration/validation uses
dual_probability_calibration.walk_forward_split() (never a random
shuffle).

Real numbers only: sample sizes, Brier scores, and per-bucket hit rates
below are computed from whatever labels the real archive actually
produces -- never fabricated, never forced to look better by widening
distances or cherry-picking a window.
"""
import argparse
import dataclasses

import numpy as np
import pandas as pd

from agents.quant_researcher import data_access, features as qr_features
from agents.trading_intelligence.dual_probability_calibration import (
    CalibratedProbabilityModel, walk_forward_split,
)
from agents.trading_intelligence.dual_probability_features import extract_feature_groups
from agents.trading_intelligence.dual_probability_labels import label_entry

FEATURE_NAMES = ["trend", "momentum", "structure", "oi", "regime_numeric"]
REGIME_ENCODING = {"TRENDING": 1.0, "RANGING": -1.0, "TRANSITIONING": 0.0, "UNKNOWN": 0.0}
DEFAULT_HORIZON_BARS = 30
DEFAULT_TARGET_ATR_MULT = 1.5
DEFAULT_STOP_ATR_MULT = 0.75
CONFIDENCE_BUCKETS = (
    (0.0, 0.50), (0.50, 0.70),  # below-threshold context, so an honest "predictions cluster low" finding is visible
    (0.70, 0.85), (0.85, 0.90), (0.90, 0.95), (0.95, 1.01),
)


def _feature_row(fg) -> list:
    return [
        fg.trend if fg.trend is not None else 0.0,
        fg.momentum if fg.momentum is not None else 0.0,
        fg.structure if fg.structure is not None else 0.0,
        fg.oi if fg.oi is not None else 0.0,
        REGIME_ENCODING.get(fg.regime, 0.0),
    ]


@dataclasses.dataclass
class DatasetRow:
    entry_idx: int
    direction: str
    features: list
    target_event: bool | None
    stop_safety_event: bool
    group_count: int


def build_dataset(symbol: str, *, horizon_bars: int = DEFAULT_HORIZON_BARS,
                   target_atr_mult: float = DEFAULT_TARGET_ATR_MULT,
                   stop_atr_mult: float = DEFAULT_STOP_ATR_MULT,
                   cycles: list | None = None) -> list[DatasetRow]:
    """Real walk-forward dataset for one symbol. Returns [] if there's no
    candle archive (never raises)."""
    candles = data_access.load_candles(symbol, timeframe="3m")
    if candles is None or candles.empty or len(candles) < horizon_bars * 3:
        return []

    ctx = qr_features.FeatureContext(candles=candles, cycles=cycles)
    atr_series = qr_features.atr(ctx)

    rows: list[DatasetRow] = []
    n = len(candles)
    idx = 200  # skip the ADX/ATR warm-up window so early bars aren't all-NaN
    while idx < n - horizon_bars - 1:
        atr_val = atr_series.iloc[idx]
        if pd.isna(atr_val) or atr_val <= 0:
            idx += 1
            continue

        fg = extract_feature_groups(ctx, idx)
        feat_row = _feature_row(fg)
        target_distance = float(atr_val) * target_atr_mult
        stop_distance = float(atr_val) * stop_atr_mult

        for direction in ("long", "short"):
            lbl = label_entry(candles, idx + 1, direction=direction,
                               target_distance=target_distance, stop_distance=stop_distance,
                               horizon_bars=horizon_bars)
            if lbl is None or lbl.truncated:
                continue
            rows.append(DatasetRow(
                entry_idx=idx + 1, direction=direction, features=feat_row,
                target_event=lbl.target_event, stop_safety_event=lbl.stop_safety_event,
                group_count=fg.group_count(),
            ))

        idx += horizon_bars  # non-overlapping, matches strategy_runner.run_strategy()'s convention

    return rows


REAL_SIGNAL_HORIZON_BARS = 10  # backtest.MAX_HOLD_MINUTES=30 / 3-minute bars -- this repo's own
                                # established intraday holding-period convention, not a new number.


DEFAULT_DEDUP_COOLDOWN_MINUTES = 30  # matches the original horizon_bars=10 baseline's implied
                                      # cooldown (3*10) -- kept as the default so h=10 behavior is
                                      # unchanged; now an independent parameter instead of being
                                      # silently derived from horizon_bars (see the horizon-sweep
                                      # finding this decouples: widening horizon_bars used to widen
                                      # the cooldown too, destroying sample size faster than the
                                      # PENDING-rate improvement could compensate).


def build_dataset_from_real_signals(symbol: str, *, date_from: str, date_to: str,
                                     horizon_bars: int = REAL_SIGNAL_HORIZON_BARS,
                                     dedup_cooldown_minutes: int = DEFAULT_DEDUP_COOLDOWN_MINUTES) -> list[DatasetRow]:
    """PHASE 2: builds the dataset from GENUINE historical signals --
    cycles.signal_direction/signal_entry/signal_target/signal_sl, written
    live by app.py's log_cycle_to_db() every production cycle directly
    from oi_engine.generate_signal()'s real output (confirmed by reading
    app.py:3138-3150; not a replay or synthetic reconstruction). Only
    rows with signal_tradeable=1 and a real CE/PE direction are used --
    the same "was this cycle actually a signal" gate the live dashboard
    itself uses.

    Target/stop distances come from the REAL signal_target/signal_sl the
    system actually proposed (not ATR-scaled guesses like
    build_dataset()'s arbitrary-bar mode) -- this is what makes this
    Phase 2, not a variant of Phase 1.

    Deduplication: cycles poll every 7-15s and a signal condition can
    persist across many consecutive cycles without the underlying
    opportunity actually changing -- treating each re-fire as an
    independent sample would inflate sample_size with correlated,
    non-independent observations of the same event (the same problem
    institutional_flow_backtest.py's own cooldown solves). Once a
    (direction) event is captured, no new event for that SAME direction
    is accepted until `dedup_cooldown_minutes` has elapsed --
    DELIBERATELY INDEPENDENT of `horizon_bars` (an earlier version tied
    cooldown to horizon_bars directly, which meant widening the horizon
    to reduce the PENDING rate also shrank the cooldown-limited sample
    count proportionally, destroying more sample than the PENDING-rate
    improvement recovered -- confirmed by a real horizon sweep, not a
    guess).
    """
    import backtest

    candles = data_access.load_candles(symbol, timeframe="3m")
    if candles is None or candles.empty:
        return []

    raw_cycles = backtest.load_cycles(symbol, date_from, date_to)
    if not raw_cycles:
        return []

    candle_datetimes = candles["datetime"]
    ctx = qr_features.FeatureContext(candles=candles, cycles=None)  # per-signal cycles context built below

    rows: list[DatasetRow] = []
    last_accepted_ts: dict[str, pd.Timestamp] = {}
    cooldown = pd.Timedelta(minutes=dedup_cooldown_minutes)

    for entry in raw_cycles:
        cycle = entry.get("cycle") or {}
        if not cycle.get("signal_tradeable"):
            continue
        sig_dir_raw = cycle.get("signal_direction")
        if sig_dir_raw not in ("CE", "PE"):
            continue
        direction = "long" if sig_dir_raw == "CE" else "short"
        entry_price = cycle.get("signal_entry")
        target_price = cycle.get("signal_target")
        sl_price = cycle.get("signal_sl")
        if entry_price is None or target_price is None or sl_price is None:
            continue
        target_distance = abs(float(target_price) - float(entry_price))
        stop_distance = abs(float(entry_price) - float(sl_price))
        if target_distance <= 0 or stop_distance <= 0:
            continue

        ts_raw = cycle.get("ts")
        if not ts_raw:
            continue
        cycle_ts = pd.Timestamp(ts_raw)

        prior = last_accepted_ts.get(direction)
        if prior is not None and cycle_ts - prior < cooldown:
            continue

        candle_idx = int(candle_datetimes.searchsorted(cycle_ts, side="left"))
        if candle_idx >= len(candles):
            continue

        fg = extract_feature_groups(ctx, candle_idx)
        # OI group specifically: read directly off THIS real signal's own
        # cycle (already have it in hand), rather than re-deriving from a
        # separately-joined cycles series -- simpler and exactly as
        # point-in-time-correct, since this cycle IS the as-of source.
        oi_val = None
        pcr = cycle.get("pcr")
        if pcr is not None:
            try:
                oi_val = float(pcr) - 1.0  # centered around 0, same sign convention as oi_delta_bias
            except (TypeError, ValueError):
                oi_val = None
        feat_row = [
            fg.trend if fg.trend is not None else 0.0,
            fg.momentum if fg.momentum is not None else 0.0,
            fg.structure if fg.structure is not None else 0.0,
            oi_val if oi_val is not None else 0.0,
            REGIME_ENCODING.get(fg.regime, 0.0),
        ]

        lbl = label_entry(candles, candle_idx, direction=direction,
                           target_distance=target_distance, stop_distance=stop_distance,
                           horizon_bars=horizon_bars)
        if lbl is None or lbl.truncated:
            continue

        rows.append(DatasetRow(
            entry_idx=candle_idx, direction=direction, features=feat_row,
            target_event=lbl.target_event, stop_safety_event=lbl.stop_safety_event,
            group_count=fg.group_count() + (1 if oi_val is not None else 0),
        ))
        last_accepted_ts[direction] = cycle_ts

    return rows


def run_calibration_report_real_signals(symbol: str, *, date_from: str, date_to: str,
                                         horizon_bars: int = REAL_SIGNAL_HORIZON_BARS,
                                         dedup_cooldown_minutes: int = DEFAULT_DEDUP_COOLDOWN_MINUTES,
                                         min_sample_size: int = CalibratedProbabilityModel.MIN_SAMPLE_SIZE_DEFAULT) -> dict:
    rows = build_dataset_from_real_signals(symbol, date_from=date_from, date_to=date_to,
                                            horizon_bars=horizon_bars, dedup_cooldown_minutes=dedup_cooldown_minutes)
    report = {
        "symbol": symbol, "horizon_bars": horizon_bars, "dedup_cooldown_minutes": dedup_cooldown_minutes,
        "total_rows": len(rows), "source": "real_oi_engine_signals", "date_from": date_from, "date_to": date_to,
    }
    if len(rows) < min_sample_size * 3:
        report["status"] = "INSUFFICIENT_DATA"
        report["reason"] = f"only {len(rows)} deduplicated real-signal rows, need at least {min_sample_size * 3} for a 3-way walk-forward split"
        return report

    report["status"] = "OK"
    report["directions"] = _fit_directions_report(rows, min_sample_size=min_sample_size)
    return report


def _reliability_table(y_true: np.ndarray, y_pred: np.ndarray) -> list[dict]:
    """Per-bucket predicted-vs-actual table -- the honesty check: if the
    model says 95%, the actual realized frequency in that bucket should
    be close to 95%, not merely internally-consistent."""
    table = []
    for lo, hi in CONFIDENCE_BUCKETS:
        mask = (y_pred >= lo) & (y_pred < hi)
        n = int(mask.sum())
        if n == 0:
            table.append({"bucket": f"{lo:.0%}-{hi:.0%}", "sample_size": 0,
                           "predicted_mean": None, "actual_rate": None})
            continue
        table.append({
            "bucket": f"{lo:.0%}-{hi:.0%}", "sample_size": n,
            "predicted_mean": round(float(y_pred[mask].mean()), 4),
            "actual_rate": round(float(y_true[mask].mean()), 4),
        })
    return table


def _fit_directions_report(rows: list[DatasetRow], *, min_sample_size: int) -> dict:
    """Shared fitting/reporting logic for both dataset sources (arbitrary
    every-bar sampling and real historical oi_engine signals) -- same
    walk-forward split, same two models, same honest reporting either
    way, so results are comparable across the two dataset-construction
    methods."""
    directions = {}
    for direction in ("long", "short"):
        sub = [r for r in rows if r.direction == direction]
        if len(sub) < min_sample_size * 3:
            directions[direction] = {"status": "INSUFFICIENT_DATA", "sample_size": len(sub)}
            continue

        X = np.array([r.features for r in sub], dtype=float)

        # ---- TARGET model: exclude PENDING (target_event is None) rows ----
        target_mask = np.array([r.target_event is not None for r in sub])
        X_t = X[target_mask]
        y_t = np.array([float(r.target_event) for r in sub if r.target_event is not None])
        train_t, calib_t, valid_t = walk_forward_split(len(y_t))
        target_model = CalibratedProbabilityModel(min_sample_size=min_sample_size).fit(
            X_t[train_t], y_t[train_t], X_t[calib_t], y_t[calib_t]
        )
        target_report = dataclasses.asdict(target_model.result)
        target_report["brier_score_calibration_set"] = target_report.pop("brier_score")
        if target_model.result.calibration_valid and (valid_t.stop - valid_t.start) > 0:
            preds = target_model.predict(X_t[valid_t])
            target_report["reliability_validation_set"] = _reliability_table(y_t[valid_t], preds)
            target_report["brier_score_holdout_validation_set"] = float(np.mean((preds - y_t[valid_t]) ** 2))
            target_report["holdout_sample_size"] = int(valid_t.stop - valid_t.start)

        # ---- STOP-SAFETY model: always defined, no PENDING case ----
        y_s = np.array([float(r.stop_safety_event) for r in sub])
        train_s, calib_s, valid_s = walk_forward_split(len(y_s))
        stop_model = CalibratedProbabilityModel(min_sample_size=min_sample_size).fit(
            X[train_s], y_s[train_s], X[calib_s], y_s[calib_s]
        )
        stop_report = dataclasses.asdict(stop_model.result)
        stop_report["brier_score_calibration_set"] = stop_report.pop("brier_score")
        if stop_model.result.calibration_valid and (valid_s.stop - valid_s.start) > 0:
            preds = stop_model.predict(X[valid_s])
            stop_report["reliability_validation_set"] = _reliability_table(y_s[valid_s], preds)
            stop_report["brier_score_holdout_validation_set"] = float(np.mean((preds - y_s[valid_s]) ** 2))
            stop_report["holdout_sample_size"] = int(valid_s.stop - valid_s.start)

        directions[direction] = {
            "sample_size": len(sub),
            "target_pending_count": int((~target_mask).sum()),
            "target_probability_model": target_report,
            "stop_safety_probability_model": stop_report,
        }
    return directions


def run_calibration_report(symbol: str, *, horizon_bars: int = DEFAULT_HORIZON_BARS,
                            min_sample_size: int = CalibratedProbabilityModel.MIN_SAMPLE_SIZE_DEFAULT) -> dict:
    rows = build_dataset(symbol, horizon_bars=horizon_bars)
    report = {"symbol": symbol, "horizon_bars": horizon_bars, "total_rows": len(rows), "source": "arbitrary_every_bar"}
    if len(rows) < min_sample_size * 3:  # need enough for train+calib+valid all to clear the floor
        report["status"] = "INSUFFICIENT_DATA"
        report["reason"] = f"only {len(rows)} labeled rows, need at least {min_sample_size * 3} for a 3-way walk-forward split"
        return report

    report["status"] = "OK"
    report["directions"] = _fit_directions_report(rows, min_sample_size=min_sample_size)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dual-probability shadow-model calibration backtest (read-only)")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--source", choices=["arbitrary", "real_signals"], default="real_signals")
    parser.add_argument("--horizon-bars", type=int, default=None)
    parser.add_argument("--date-from", default="2026-07-13")
    parser.add_argument("--date-to", default="2026-08-18")
    args = parser.parse_args()

    import json
    if args.source == "real_signals":
        result = run_calibration_report_real_signals(
            args.symbol, date_from=args.date_from, date_to=args.date_to,
            horizon_bars=args.horizon_bars or REAL_SIGNAL_HORIZON_BARS,
        )
    else:
        result = run_calibration_report(args.symbol, horizon_bars=args.horizon_bars or DEFAULT_HORIZON_BARS)
    print(json.dumps(result, indent=2, default=str))
