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


def run_calibration_report(symbol: str, *, horizon_bars: int = DEFAULT_HORIZON_BARS,
                            min_sample_size: int = CalibratedProbabilityModel.MIN_SAMPLE_SIZE_DEFAULT) -> dict:
    rows = build_dataset(symbol, horizon_bars=horizon_bars)
    report = {"symbol": symbol, "horizon_bars": horizon_bars, "total_rows": len(rows)}
    if len(rows) < min_sample_size * 3:  # need enough for train+calib+valid all to clear the floor
        report["status"] = "INSUFFICIENT_DATA"
        report["reason"] = f"only {len(rows)} labeled rows, need at least {min_sample_size * 3} for a 3-way walk-forward split"
        return report

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

    report["status"] = "OK"
    report["directions"] = directions
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Dual-probability shadow-model calibration backtest (read-only)")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--horizon-bars", type=int, default=DEFAULT_HORIZON_BARS)
    args = parser.parse_args()

    import json
    result = run_calibration_report(args.symbol, horizon_bars=args.horizon_bars)
    print(json.dumps(result, indent=2, default=str))
