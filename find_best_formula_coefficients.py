"""
find_best_formula_coefficients.py -- empirical coefficient search.

Given the 247-day evidence that the current formula (PDH+Range/2 for
resistance, PDH-Range/4 for resistance_reversal, mirrored for support) is
consistently biased (outer levels too far, inner levels too close), this
searches over a range of candidate divisors to find which one comes closest
to UNBIASED (average gap near zero) against the actual 6-month history.

This does NOT change any live formula -- it's a read-only analysis tool.
Any adopted change must be made deliberately in market_structure.py after
reviewing these results.

USAGE:
    ./venv/bin/python3 find_best_formula_coefficients.py --symbol NIFTY
"""
import argparse
import pandas as pd
from pathlib import Path

DATA_DIR = Path("data/history")


def load_candles(symbol, timeframe="3m"):
    pq_path = DATA_DIR / symbol / f"{timeframe}.parquet"
    csv_path = DATA_DIR / symbol / f"{timeframe}.csv"
    if pq_path.exists():
        df = pd.read_parquet(pq_path)
    elif csv_path.exists():
        df = pd.read_csv(csv_path, parse_dates=["datetime"])
    else:
        return None
    df["datetime"] = pd.to_datetime(df["datetime"])
    return df.sort_values("datetime").reset_index(drop=True)


def build_daily_levels(candles):
    """Returns a list of (pdh, pdl, pdc, range_, day_high, day_low) tuples,
    one per trading day (using the PREVIOUS day's candles for PDH/PDL/PDC)."""
    candles = candles.copy()
    candles["date"] = candles["datetime"].dt.date
    trading_days = sorted(candles["date"].unique())
    daily = []
    for i in range(1, len(trading_days)):
        prev_day, today = trading_days[i - 1], trading_days[i]
        prev_c = candles[candles["date"] == prev_day]
        today_c = candles[candles["date"] == today]
        if len(prev_c) == 0 or len(today_c) == 0:
            continue
        pdh, pdl, pdc = prev_c["high"].max(), prev_c["low"].min(), prev_c["close"].iloc[-1]
        daily.append({
            "pdh": pdh, "pdl": pdl, "pdc": pdc, "range": pdh - pdl,
            "day_high": today_c["high"].max(), "day_low": today_c["low"].min(),
        })
    return daily


def evaluate_divisor(daily, level_fn, divisor, is_resistance_side):
    gaps = []
    for day in daily:
        level = level_fn(day["pdh"], day["pdl"], day["pdc"], day["range"], divisor)
        actual = day["day_high"] if is_resistance_side else day["day_low"]
        gap = (level - actual) if is_resistance_side else (actual - level)
        gaps.append(gap)
    avg = sum(gaps) / len(gaps) if gaps else None
    return avg


def find_best(daily, level_fn, is_resistance_side, min_divisor=1.0, max_divisor=8.0, step=0.25):
    best_divisor, best_abs_bias, best_avg = None, float("inf"), None
    d = min_divisor
    while d <= max_divisor:
        avg = evaluate_divisor(daily, level_fn, d, is_resistance_side)
        if avg is not None and abs(avg) < best_abs_bias:
            best_abs_bias, best_divisor, best_avg = abs(avg), d, avg
        d += step
    if best_divisor is not None and best_divisor >= max_divisor - step:
        print(f"  ⚠️  WARNING: best-fit divisor ({best_divisor}) hit the search boundary "
              f"(max={max_divisor}) -- the true optimum is likely BEYOND this range. "
              f"Re-run with a higher max_divisor before trusting this result.")
    return best_divisor, best_avg


def main():
    parser = argparse.ArgumentParser(description="Empirical S/R formula coefficient search")
    parser.add_argument("--symbol", type=str, required=True)
    parser.add_argument("--timeframe", type=str, default="3m")
    parser.add_argument("--max-divisor", type=float, default=30.0,
                         help="Upper bound for the divisor search -- raise this if a prior run hit the boundary.")
    args = parser.parse_args()

    candles = load_candles(args.symbol, args.timeframe)
    if candles is None:
        print(f"No historical data for {args.symbol} -- run fetch_history.py first.")
        return
    daily = build_daily_levels(candles)
    print(f"Evaluating {len(daily)} trading days for {args.symbol}...\n")

    # Outer levels: currently PDH + Range/divisor (resistance), PDL - Range/divisor (support)
    outer_res_fn = lambda pdh, pdl, pdc, r, d: pdh + r / d
    outer_sup_fn = lambda pdh, pdl, pdc, r, d: pdl - r / d
    # Inner (reversal) levels: currently PDH - Range/divisor (resistance_reversal), PDL + Range/divisor (support_reversal)
    inner_res_fn = lambda pdh, pdl, pdc, r, d: pdh - r / d
    inner_sup_fn = lambda pdh, pdl, pdc, r, d: pdl + r / d

    print("=== RESISTANCE (outer) -- currently Range/2 ===")
    d, avg = find_best(daily, outer_res_fn, is_resistance_side=True, max_divisor=args.max_divisor)
    print(f"  Best-fit divisor: Range/{d}  (current: Range/2)")
    print(f"  Resulting average bias: {avg:+.2f} pts (near-zero = unbiased)\n")

    print("=== SUPPORT (outer) -- currently Range/2 ===")
    d, avg = find_best(daily, outer_sup_fn, is_resistance_side=False, max_divisor=args.max_divisor)
    print(f"  Best-fit divisor: Range/{d}  (current: Range/2)")
    print(f"  Resulting average bias: {avg:+.2f} pts (near-zero = unbiased)\n")

    print("=== RESISTANCE_REVERSAL (inner) -- currently Range/4 ===")
    d, avg = find_best(daily, inner_res_fn, is_resistance_side=True, max_divisor=args.max_divisor)
    print(f"  Best-fit divisor: Range/{d}  (current: Range/4)")
    print(f"  Resulting average bias: {avg:+.2f} pts (near-zero = unbiased)\n")

    print("=== SUPPORT_REVERSAL (inner) -- currently Range/4 ===")
    d, avg = find_best(daily, inner_sup_fn, is_resistance_side=False, max_divisor=args.max_divisor)
    print(f"  Best-fit divisor: Range/{d}  (current: Range/4)")
    print(f"  Resulting average bias: {avg:+.2f} pts (near-zero = unbiased)\n")

    print("=== CONTROL CHECK: plain PDH / PDL, NO Range-adjustment at all ===")
    print("  (bias kept shrinking as the divisor grew -- this tests the limiting case directly)")
    pdh_only_fn = lambda pdh, pdl, pdc, r, d: pdh
    pdl_only_fn = lambda pdh, pdl, pdc, r, d: pdl
    avg_pdh = evaluate_divisor(daily, pdh_only_fn, 1, is_resistance_side=True)
    avg_pdl = evaluate_divisor(daily, pdl_only_fn, 1, is_resistance_side=False)
    print(f"  Plain PDH vs day_high: {avg_pdh:+.2f} pts average bias")
    print(f"  Plain PDL vs day_low:  {avg_pdl:+.2f} pts average bias\n")

    print("=" * 70)
    print("These are UNBIASED-average-fit divisors from 6-month history -- a")
    print("starting point for discussion, not an automatic change. Review")
    print("before editing market_structure.py's custom_range_levels().")
    print("=" * 70)


if __name__ == "__main__":
    main()
