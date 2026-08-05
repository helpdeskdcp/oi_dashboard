"""
analyze_swing_low_patterns.py -- Detects genuine swing-low points in a
symbol's 1-year historical candle-data, and honestly measures what
actually happened to price afterward (points moved, over several
time-windows) -- real historical statistics, NOT a prediction.

Reuses find_reversal_points() from app.py (the SAME trough-detection logic
already used live for chart visualization) rather than duplicating it.

USAGE:
    ./venv/bin/python3 analyze_swing_low_patterns.py --symbol NATURALGAS
"""
import argparse
import statistics
import sys
sys.path.insert(0, ".")

import pandas as pd
from pathlib import Path

from app import find_reversal_points

DATA_DIR = Path("data/history")

# How far forward (in candles) to measure the outcome after each swing-low.
# At 3-minute candles: 20=~1hr, 50=~2.5hr, 100=~5hr (roughly one session).
FORWARD_WINDOWS = {"1hr": 20, "2.5hr": 50, "~1_session": 100}


def load_close_series(symbol, timeframe="3m"):
    pq_path = DATA_DIR / symbol / f"{timeframe}.parquet"
    csv_path = DATA_DIR / symbol / f"{timeframe}.csv"
    if pq_path.exists():
        df = pd.read_parquet(pq_path)
    elif csv_path.exists():
        df = pd.read_csv(csv_path, parse_dates=["datetime"])
    else:
        return None
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.sort_values("datetime").reset_index(drop=True)
    return df


def analyze(symbol, window=3):
    df = load_close_series(symbol)
    if df is None or len(df) < 200:
        print(f"{symbol}: no usable candle-history (need fetch_history.py first).")
        return

    closes = df["close"].tolist()
    print(f"Analyzing {len(closes)} candles for {symbol}...")

    troughs = [p for p in find_reversal_points(closes, window=window) if p["type"] == "trough"]
    print(f"Detected {len(troughs)} genuine swing-low points.\n")

    if not troughs:
        print("No swing-lows detected -- try a smaller --window.")
        return

    outcomes = {label: [] for label in FORWARD_WINDOWS}

    for t in troughs:
        idx = t["index"]
        low_price = t["value"]
        # BUG FIX: the outcome-window must start AFTER the detection-window
        # ends, not immediately after the trough itself -- otherwise "moved
        # up" is tautologically true by the very definition of a trough
        # (price is compared against its neighbors within `window` candles
        # on each side, so of course it looks "up" within that same span).
        measure_start = idx + window + 1
        for label, n_candles in FORWARD_WINDOWS.items():
            end_idx = measure_start + n_candles
            if end_idx >= len(closes):
                continue   # not enough forward-data for this swing-low yet
            future_prices = closes[measure_start:end_idx + 1]
            if not future_prices:
                continue
            max_future = max(future_prices)
            points_moved = max_future - low_price
            outcomes[label].append(points_moved)

    print(f"{'Window':12} {'Sample':>8} {'Avg-pts-up':>12} {'Median-pts-up':>15} {'% genuinely moved up >0':>26}")
    for label, moves in outcomes.items():
        if not moves:
            print(f"{label:12} {'(no data yet -- too close to end of history)'}")
            continue
        avg = statistics.mean(moves)
        median = statistics.median(moves)
        pct_positive = sum(1 for m in moves if m > 0) / len(moves) * 100
        print(f"{label:12} {len(moves):>8} {avg:>12.2f} {median:>15.2f} {pct_positive:>25.1f}%")

    print("\nHONEST NOTE: 'points moved up' measures the MAX price reached in the")
    print("forward window after each swing-low -- not a guarantee any specific")
    print("trade would have captured that full move (entry/exit timing, slippage,")
    print("and premium (not underlying) behavior are NOT modeled here). This is")
    print("descriptive historical statistics, not a backtested trading strategy.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", type=str, required=True)
    parser.add_argument("--window", type=int, default=3, help="Local-extrema window (candles on each side)")
    args = parser.parse_args()
    analyze(args.symbol.upper(), window=args.window)
