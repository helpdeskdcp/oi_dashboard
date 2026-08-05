"""
analyze_standard_formulas.py -- backtest 2 industry-standard S/R formulas.

Tests the two GLOBALLY-RECOGNIZED, textbook formulas (as documented across
trading platforms worldwide) against our 6-month historical OHLC data,
using the SAME methodology as find_best_formula_coefficients.py.

STANDALONE and READ-ONLY -- does NOT modify or touch the live application,
market_structure.py's formula, or engine_v2.py in any way. This is purely
an evidence-gathering tool to inform a FUTURE, deliberate decision.

Formulas tested:
  1. Standard Pivot Points (the original floor-trader formula):
       P  = (PDH + PDL + PDC) / 3
       R1 = 2*P - PDL
       S1 = 2*P - PDH
       R2 = P + (PDH - PDL)
       S2 = P - (PDH - PDL)

  2. CPR -- Central Pivot Range:
       P  = (PDH + PDL + PDC) / 3
       BC = (PDH + PDL) / 2
       TC = (P - BC) + P
     Plus the standard "narrow vs wide CPR" heuristic: a narrow TC-BC gap
     is said to predict a bigger move that day; a wide gap predicts a
     rangebound/reversal-prone day. We test this correlation too.

NOT tested here: the 3rd formula (Implied-Volatility-based Expected Move)
-- this needs historical IV data, which our `strikes` table does not store
(only OI/Volume/LTP are logged, not IV). Live IV IS available (ce_iv/pe_iv
in bse_fetcher.py) but was never persisted historically, so this formula
genuinely cannot be backtested with what we have; it would need forward-only
tracking from today onward.

USAGE:
    ./venv/bin/python3 analyze_standard_formulas.py --symbol NIFTY
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


def standard_pivot_levels(pdh, pdl, pdc):
    p = (pdh + pdl + pdc) / 3
    r1 = 2 * p - pdl
    s1 = 2 * p - pdh
    r2 = p + (pdh - pdl)
    s2 = p - (pdh - pdl)
    return {"pivot": p, "r1": r1, "s1": s1, "r2": r2, "s2": s2}


def cpr_levels(pdh, pdl, pdc):
    p = (pdh + pdl + pdc) / 3
    bc = (pdh + pdl) / 2
    tc = (p - bc) + p
    return {"pivot": p, "bc": bc, "tc": tc, "width": abs(tc - bc)}


def analyze(symbol, timeframe="3m", min_approach_pct=1.0):
    candles = load_candles(symbol, timeframe)
    if candles is None or len(candles) == 0:
        print(f"No historical data found for {symbol} [{timeframe}] -- run fetch_history.py first.")
        return

    candles["date"] = candles["datetime"].dt.date
    trading_days = sorted(candles["date"].unique())
    print(f"Found {len(trading_days)} trading days of history for {symbol} [{timeframe}].\n")

    pivot_gaps = {"r1": [], "s1": [], "r2": [], "s2": []}
    cpr_gaps = {"tc": [], "bc": []}
    cpr_width_vs_range = []   # (width, day_range) pairs -- to test "narrow CPR = bigger move" heuristic

    for i in range(1, len(trading_days)):
        prev_day, today = trading_days[i - 1], trading_days[i]
        prev_candles = candles[candles["date"] == prev_day]
        today_candles = candles[candles["date"] == today]
        if len(prev_candles) == 0 or len(today_candles) == 0:
            continue

        pdh, pdl, pdc = prev_candles["high"].max(), prev_candles["low"].min(), prev_candles["close"].iloc[-1]
        day_high, day_low = today_candles["high"].max(), today_candles["low"].min()
        day_range = day_high - day_low

        pivots = standard_pivot_levels(pdh, pdl, pdc)
        min_approach = pivots["r1"] * (min_approach_pct / 100.0)
        if abs(pivots["r1"] - day_high) <= min_approach:
            pivot_gaps["r1"].append(pivots["r1"] - day_high)
        if abs(day_low - pivots["s1"]) <= min_approach:
            pivot_gaps["s1"].append(day_low - pivots["s1"])
        if abs(pivots["r2"] - day_high) <= min_approach:
            pivot_gaps["r2"].append(pivots["r2"] - day_high)
        if abs(day_low - pivots["s2"]) <= min_approach:
            pivot_gaps["s2"].append(day_low - pivots["s2"])

        cpr = cpr_levels(pdh, pdl, pdc)
        if abs(cpr["tc"] - day_high) <= min_approach:
            cpr_gaps["tc"].append(cpr["tc"] - day_high)
        if abs(day_low - cpr["bc"]) <= min_approach:
            cpr_gaps["bc"].append(day_low - cpr["bc"])
        cpr_width_vs_range.append((cpr["width"], day_range))

    def summarize(name, gaps):
        print(f"=== {name} ===")
        if not gaps:
            print("  No days where price approached this level closely enough to measure.\n")
            return
        avg_gap = sum(gaps) / len(gaps)
        short_count = sum(1 for v in gaps if v > 0)
        past_count = sum(1 for v in gaps if v < 0)
        print(f"  {len(gaps)} day(s) approached within {min_approach_pct}% "
              f"({short_count} fell short, {past_count} were crossed)")
        print(f"  Average gap: {avg_gap:+.2f} pts "
              f"({'consistently falls short' if avg_gap > 0.3 else 'consistently exceeded' if avg_gap < -0.3 else 'roughly balanced, no clear bias'})")
        print()

    print("--- FORMULA 1: Standard Pivot Points ---")
    summarize("R1 (2P - PDL)", pivot_gaps["r1"])
    summarize("S1 (2P - PDH)", pivot_gaps["s1"])
    summarize("R2 (P + Range)", pivot_gaps["r2"])
    summarize("S2 (P - Range)", pivot_gaps["s2"])

    print("--- FORMULA 2: CPR (Central Pivot Range) ---")
    summarize("TC (Top Central)", cpr_gaps["tc"])
    summarize("BC (Bottom Central)", cpr_gaps["bc"])

    print("--- CPR Width vs Day-Range correlation (the 'narrow CPR = bigger move' heuristic) ---")
    if len(cpr_width_vs_range) >= 5:
        widths = [w for w, r in cpr_width_vs_range]
        ranges = [r for w, r in cpr_width_vs_range]
        median_width = sorted(widths)[len(widths) // 2]
        narrow_ranges = [r for w, r in cpr_width_vs_range if w <= median_width]
        wide_ranges = [r for w, r in cpr_width_vs_range if w > median_width]
        avg_narrow_range = sum(narrow_ranges) / len(narrow_ranges) if narrow_ranges else 0
        avg_wide_range = sum(wide_ranges) / len(wide_ranges) if wide_ranges else 0
        print(f"  Median CPR width: {median_width:.2f}")
        print(f"  Avg day-range when CPR is NARROW (below median): {avg_narrow_range:.2f} pts ({len(narrow_ranges)} days)")
        print(f"  Avg day-range when CPR is WIDE (above median): {avg_wide_range:.2f} pts ({len(wide_ranges)} days)")
        if avg_narrow_range > avg_wide_range:
            print("  -> Heuristic HOLDS on this data: narrow-CPR days genuinely had bigger moves.")
        else:
            print("  -> Heuristic does NOT hold on this data: narrow-CPR days did not have bigger moves.")
    else:
        print("  Not enough days to test this correlation.")

    print()
    print("=" * 70)
    print(f"Sample size: {len(trading_days)-1} trading days")
    print("Formula-accuracy ONLY -- standalone analysis, does not affect the live app.")
    print("Formula 3 (IV-based Expected Move) skipped -- historical IV data isn't stored.")
    print("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backtest Standard Pivot Points and CPR formulas using fetched OHLCV history")
    parser.add_argument("--symbol", type=str, required=True)
    parser.add_argument("--timeframe", type=str, default="3m")
    parser.add_argument("--min-approach-pct", type=float, default=1.0)
    args = parser.parse_args()
    analyze(args.symbol, args.timeframe, args.min_approach_pct)
