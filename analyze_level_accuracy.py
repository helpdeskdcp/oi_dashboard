"""
analyze_level_accuracy.py -- data-driven formula-tuning analysis.

Does NOT change any trading logic. Read-only. Examines every day of
available historical data (market_structure_snapshots + cycles) to check:
for days where price got reasonably close to our calculated resistance/
support/reversal levels, how far short (or past) did the ACTUAL peak/trough
fall compared to our CALCULATED level?

If there's a consistent, sizeable gap across many days, that's genuine
evidence worth tuning the PDH/PDL range-fraction formula on. A single day's
anecdote is not -- this script is how we tell the difference.

USAGE (on VPS, in ~/oi_dashboard):
    ./venv/bin/python3 analyze_level_accuracy.py [--symbol SYMBOL] [--min-approach 5]
"""
import sqlite3
import json
import argparse
from collections import defaultdict

DB_PATH = "oi_history.db"


def analyze(symbol_filter=None, min_approach_pct=1.0):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    query = "SELECT DISTINCT symbol, date FROM market_structure_snapshots"
    params = []
    if symbol_filter:
        query += " WHERE symbol = ?"
        params.append(symbol_filter)
    days = conn.execute(query, params).fetchall()

    print(f"Found {len(days)} (symbol, date) combinations with logged snapshots.\n")

    # gaps[level_type] = list of (calculated_level - actual_extreme) in points,
    # only for days where price got within min_approach_pct% of the level
    resistance_gaps = []
    resistance_reversal_gaps = []
    support_gaps = []
    support_reversal_gaps = []

    for row in days:
        symbol, date = row["symbol"], row["date"]
        snap = conn.execute(
            "SELECT custom_levels_json FROM market_structure_snapshots WHERE symbol=? AND date=? LIMIT 1",
            (symbol, date),
        ).fetchone()
        if not snap or not snap["custom_levels_json"]:
            continue
        levels = json.loads(snap["custom_levels_json"])
        resistance = levels.get("resistance")
        resistance_reversal = levels.get("resistance_reversal")
        support = levels.get("support")
        support_reversal = levels.get("support_reversal")
        if not all([resistance, resistance_reversal, support, support_reversal]):
            continue

        prices = [r["underlying_ltp"] for r in conn.execute(
            "SELECT underlying_ltp FROM cycles WHERE symbol=? AND date=? AND underlying_ltp IS NOT NULL",
            (symbol, date),
        ).fetchall()]
        if not prices:
            continue

        day_high, day_low = max(prices), min(prices)
        min_approach = resistance * (min_approach_pct / 100.0)   # scales per-instrument price level

        # Only count days where price genuinely approached the level (within
        # min_approach_pct%, in EITHER direction) -- otherwise a day where
        # price blew massively past the level (or never got close at all)
        # would be meaningless noise, not a real accuracy signal.
        if abs(resistance - day_high) <= min_approach:
            resistance_gaps.append((symbol, date, resistance - day_high))
        if abs(resistance_reversal - day_high) <= min_approach:
            resistance_reversal_gaps.append((symbol, date, resistance_reversal - day_high))
        if abs(day_low - support) <= min_approach:
            support_gaps.append((symbol, date, day_low - support))
        if abs(day_low - support_reversal) <= min_approach:
            support_reversal_gaps.append((symbol, date, day_low - support_reversal))

    conn.close()

    def summarize(name, gaps):
        print(f"=== {name} ===")
        if not gaps:
            print("  No days where price approached this level closely enough to measure yet.\n")
            return
        values = [g[2] for g in gaps]
        avg_gap = sum(values) / len(values)
        print(f"  {len(gaps)} day(s) where price approached within {min_approach_pct}% of the level:")
        for symbol, date, gap in gaps:
            direction = "SHORT of level (never reached)" if gap > 0 else "PAST level (level was crossed)"
            print(f"    {symbol} {date}: {abs(gap):.2f} pts {direction}")
        print(f"  Average gap: {avg_gap:+.2f} pts "
              f"({'consistently falls short -- level may be too far/conservative' if avg_gap > 0.3 else 'consistently exceeded -- level may be too close/loose' if avg_gap < -0.3 else 'roughly accurate, no clear bias'})")
        print()

    summarize("RESISTANCE (outer level)", resistance_gaps)
    summarize("RESISTANCE_REVERSAL (inner level -- the one from yesterday's NATURALGAS case)", resistance_reversal_gaps)
    summarize("SUPPORT (outer level)", support_gaps)
    summarize("SUPPORT_REVERSAL (inner level)", support_reversal_gaps)

    print("=" * 70)
    print("NOTE: This is informational only -- no trading logic was changed.")
    print("A consistent bias across MANY days (not 1-2) across MULTIPLE symbols")
    print("would be the genuine signal to consider adjusting the range-fraction")
    print("in market_structure.py's custom_range_levels() calculation.")
    print("=" * 70)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Analyze S/R level accuracy against historical price action")
    parser.add_argument("--symbol", type=str, default=None, help="Filter to one symbol (default: all)")
    parser.add_argument("--min-approach-pct", type=float, default=1.0, help="Only count days price got within this %% of the level (default 1.0%%, scales naturally across cheap and expensive instruments)")
    args = parser.parse_args()
    analyze(args.symbol, args.min_approach_pct)
