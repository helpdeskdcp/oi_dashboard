"""
analyze_level_accuracy_6month.py -- large-sample formula-accuracy test.

Uses the raw OHLCV history fetched by history_engine.py (data/history/<symbol>/3m.parquet
or .csv) to test the S/R FORMULA's accuracy over 6 months (100+ trading days),
instead of the ~5 days available in market_structure_snapshots (used by
analyze_level_accuracy.py).

IMPORTANT SCOPE LIMIT: this tests the FORMULA only (does price genuinely turn
near the calculated levels?) -- it does NOT and CANNOT test the full OI-based
trading STRATEGY, since Angel One's historical API doesn't provide option-chain
history. For strategy-backtesting, only the ~13-July-onward `cycles`/`strikes`
DB data is usable (see backtest.py).

Read-only. Does not change any trading logic.

USAGE (on VPS, in ~/oi_dashboard):
    ./venv/bin/python3 analyze_level_accuracy_6month.py --symbol NIFTY
    ./venv/bin/python3 analyze_level_accuracy_6month.py --symbol NIFTY --min-approach-pct 0.5
"""
import argparse
import pandas as pd
from pathlib import Path

from market_structure import custom_range_levels

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


def analyze(symbol, timeframe="3m", min_approach_pct=1.0):
    candles = load_candles(symbol, timeframe)
    if candles is None or len(candles) == 0:
        print(f"No historical data found for {symbol} [{timeframe}] -- run fetch_history.py first.")
        return

    candles["date"] = candles["datetime"].dt.date
    trading_days = sorted(candles["date"].unique())
    print(f"Found {len(trading_days)} trading days of history for {symbol} [{timeframe}].\n")

    resistance_gaps, resistance_reversal_gaps = [], []
    support_gaps, support_reversal_gaps = [], []

    for i in range(1, len(trading_days)):
        prev_day, today = trading_days[i - 1], trading_days[i]
        prev_candles = candles[candles["date"] == prev_day]
        today_candles = candles[candles["date"] == today]
        if len(prev_candles) == 0 or len(today_candles) == 0:
            continue

        pdh, pdl, pdc = prev_candles["high"].max(), prev_candles["low"].min(), prev_candles["close"].iloc[-1]
        levels = custom_range_levels(pdh, pdl, pdc)

        day_high, day_low = today_candles["high"].max(), today_candles["low"].min()
        min_approach = levels["resistance"] * (min_approach_pct / 100.0)

        if abs(levels["resistance"] - day_high) <= min_approach:
            resistance_gaps.append((today, levels["resistance"] - day_high))
        if abs(levels["resistance_reversal"] - day_high) <= min_approach:
            resistance_reversal_gaps.append((today, levels["resistance_reversal"] - day_high))
        if abs(day_low - levels["support"]) <= min_approach:
            support_gaps.append((today, day_low - levels["support"]))
        if abs(day_low - levels["support_reversal"]) <= min_approach:
            support_reversal_gaps.append((today, day_low - levels["support_reversal"]))

    def summarize(name, gaps):
        print(f"=== {name} ===")
        if not gaps:
            print("  No days where price approached this level closely enough to measure.\n")
            return
        values = [g[1] for g in gaps]
        avg_gap = sum(values) / len(values)
        short_count = sum(1 for v in values if v > 0)
        past_count = sum(1 for v in values if v < 0)
        print(f"  {len(gaps)} day(s) approached within {min_approach_pct}% "
              f"({short_count} fell short, {past_count} were crossed)")
        print(f"  Average gap: {avg_gap:+.2f} pts "
              f"({'consistently falls short' if avg_gap > 0.3 else 'consistently exceeded' if avg_gap < -0.3 else 'roughly balanced, no clear bias'})")
        print()

    summarize("RESISTANCE (outer level)", resistance_gaps)
    summarize("RESISTANCE_REVERSAL (inner level)", resistance_reversal_gaps)
    summarize("SUPPORT (outer level)", support_gaps)
    summarize("SUPPORT_REVERSAL (inner level)", support_reversal_gaps)

    print("=" * 70)
    print(f"Sample size: {len(trading_days)-1} trading days (vs ~5 in the DB-snapshot version)")
    print("Formula-accuracy ONLY -- does not test the OI-based trading strategy")
    print("(option-chain history isn't available from Angel One's historical API).")
    print("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="6-month S/R formula-accuracy test using fetched OHLCV history")
    parser.add_argument("--symbol", type=str, required=True)
    parser.add_argument("--timeframe", type=str, default="3m")
    parser.add_argument("--min-approach-pct", type=float, default=1.0)
    args = parser.parse_args()
    analyze(args.symbol, args.timeframe, args.min_approach_pct)
