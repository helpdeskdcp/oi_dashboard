"""
backfill_market_structure.py -- Retroactively computes PDH/PDL/PDC-based
market-structure snapshots from ALREADY-FETCHED historical candle data.

WHY THIS IS SAFE:
- No new API calls -- uses ONLY the candle-history already downloaded by
  fetch_history.py (data/history/<symbol>/3m.parquet).
- No fabricated data -- PDH/PDL/PDC are trivially derivable from real OHLC
  candles (prior trading-day's high/low/close). This is NOT a guess or
  approximation; it's the same math the live app does, just applied to
  historical candles instead of live ones.
- Reuses the EXACT SAME formula (custom_range_levels from market_structure.py)
  already used by the live app and by Engine V2 -- so backtest results from
  the backfilled days genuinely reflect what the live engine would have
  computed, not a different/approximate formula.

WHAT THIS DOES NOT DO:
- Does not compute ATR-14, ADX, regime, mother-candle, or liquidity-sweep
  detection retroactively -- those need more careful historical-replay
  logic and are left as None (the backtest already handles missing/partial
  snapshot-data gracefully, falling back to pseudo-approximation only for
  what's genuinely missing).

USAGE:
    ./venv/bin/python3 backfill_market_structure.py --symbol NIFTY
    ./venv/bin/python3 backfill_market_structure.py --all
"""
import argparse
import json
import sqlite3
import pandas as pd
from pathlib import Path

from market_structure import custom_range_levels

DB_PATH = "oi_history.db"
DATA_DIR = Path("data/history")


def load_daily_ohlc(symbol, timeframe="3m"):
    """Loads candle-history and aggregates it into one row per trading-day
    (day's high, low, close-of-last-candle)."""
    pq_path = DATA_DIR / symbol / f"{timeframe}.parquet"
    csv_path = DATA_DIR / symbol / f"{timeframe}.csv"
    if pq_path.exists():
        df = pd.read_parquet(pq_path)
    elif csv_path.exists():
        df = pd.read_csv(csv_path, parse_dates=["datetime"])
    else:
        return None
    df["datetime"] = pd.to_datetime(df["datetime"])
    df["date"] = df["datetime"].dt.date
    daily = df.groupby("date").agg(
        high=("high", "max"), low=("low", "min"),
        close=("close", "last"),
    ).reset_index()
    return daily.sort_values("date").reset_index(drop=True)


def backfill_symbol(symbol, dry_run=False):
    daily = load_daily_ohlc(symbol)
    if daily is None or len(daily) < 2:
        print(f"{symbol}: no usable candle-history found (need fetch_history.py run first) -- skipping.")
        return 0

    conn = sqlite3.connect(DB_PATH)
    existing_dates = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT date FROM market_structure_snapshots WHERE symbol=?", (symbol,)
        )
    }

    inserted = 0
    for i in range(1, len(daily)):   # start at 1 -- day 0 has no "previous day"
        today_date = daily.iloc[i]["date"]
        date_str = today_date.isoformat()
        if date_str in existing_dates:
            continue   # already has a genuine (live-captured) snapshot -- don't overwrite real data with backfilled data

        prev_row = daily.iloc[i - 1]
        levels = custom_range_levels(float(prev_row["high"]), float(prev_row["low"]), float(prev_row["close"]))

        if dry_run:
            print(f"  Would insert {symbol} {date_str}: PDH={levels['pdh']} PDL={levels['pdl']} PDC={levels['pdc']}")
            inserted += 1
            continue

        conn.execute(
            """INSERT INTO market_structure_snapshots
               (symbol, date, time, ts, atr_14, adx, regime, pdh, pdl, pdc, vwap,
                swing_high, swing_low, mother_candle_json, liquidity_sweep_json, custom_levels_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            # ts MUST be this historical trading day's market-open time, NOT
            # time.time() (when the backfill script happens to run) -- PDH/PDL/PDC
            # are legitimately known at 09:00 that day (derived from the PRIOR
            # day's already-closed candle), so backdating ts here isn't lookahead
            # bias, it's correcting a bug that made backfilled snapshots look like
            # they were "computed" at backfill-run time, far AFTER every cycle on
            # that historical day -- which made simulate_v3_engine_trades's
            # lookahead-bias guard (backtest.py) reject the snapshot for 100% of
            # that day's cycles, silently killing every backfilled day's trades.
            (symbol, date_str, "09:00:00", f"{date_str}T09:00:00", None, None, None,
             levels["pdh"], levels["pdl"], levels["pdc"], None, None, None,
             json.dumps({"found": False}), json.dumps({"swept": None}), json.dumps(levels)),
        )
        inserted += 1

    conn.commit()
    conn.close()
    return inserted


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill PDH/PDL market-structure snapshots from historical candles")
    parser.add_argument("--symbol", type=str, help="Single symbol to backfill")
    parser.add_argument("--all", action="store_true", help="Backfill all symbols with available candle-history")
    parser.add_argument("--dry-run", action="store_true", help="Show what WOULD be inserted, without writing anything")
    args = parser.parse_args()

    if args.all:
        symbols = [p.name for p in DATA_DIR.iterdir() if p.is_dir()] if DATA_DIR.exists() else []
    elif args.symbol:
        symbols = [args.symbol.upper()]
    else:
        print("Specify --symbol SYMBOL or --all")
        exit(1)

    total = 0
    for sym in symbols:
        n = backfill_symbol(sym, dry_run=args.dry_run)
        print(f"{sym}: {'would backfill' if args.dry_run else 'backfilled'} {n} day(s)")
        total += n

    print(f"\nTotal: {total} day(s) {'would be ' if args.dry_run else ''}backfilled across {len(symbols)} symbol(s).")
    if not args.dry_run and total > 0:
        print("Re-run your backtest now -- it should show more days using REAL market-structure.")
