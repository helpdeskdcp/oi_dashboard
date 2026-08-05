#!/usr/bin/env python3
"""
fetch_history.py -- standalone History Engine runner.

Unlike calling `from app import AngelOneFetcher, SYMBOLS` directly (which
triggers the full live app: a second Angel One login, the background
data-fetch loop for all 14 symbols, etc. -- wasteful and risks rate-limit
contention with the ACTUAL always-running live app), this script sets
SKIP_AUTOSTART=1 BEFORE importing app.py, so only the classes/config get
loaded -- no duplicate live-loop, no duplicate background fetching.

USAGE (on VPS, in ~/oi_dashboard):

    # One-time backfill for one symbol, one timeframe:
    ./venv/bin/python3 fetch_history.py --symbol NIFTY --timeframe 3m --from 2025-07-23 --to 2026-07-23

    # Daily incremental update for one symbol (only fetches missing candles):
    ./venv/bin/python3 fetch_history.py --symbol NIFTY --timeframe 3m --update

    # Update ALL symbols, ALL timeframes (suitable for a daily cron job):
    ./venv/bin/python3 fetch_history.py --all --update

    # Verify a symbol's saved data for gaps/duplicates:
    ./venv/bin/python3 fetch_history.py --symbol NIFTY --timeframe 3m --verify

CRON EXAMPLE (daily incremental update at 6 PM IST, after both NSE and MCX close):
    0 18 * * 1-6 cd /root/oi_dashboard && ./venv/bin/python3 fetch_history.py --all --update >> logs/history_update.log 2>&1
"""
import os
os.environ["SKIP_AUTOSTART"] = "1"   # MUST be set before importing app -- see module docstring

import argparse
import sys

from app import AngelOneFetcher, SYMBOLS
from history_engine import HistoryEngine, TIMEFRAMES


def main():
    parser = argparse.ArgumentParser(description="Angel One Historical Data Engine -- standalone runner")
    parser.add_argument("--symbol", type=str, help="Symbol (e.g. NIFTY, NATURALGAS) -- omit with --all for every symbol")
    parser.add_argument("--all", action="store_true", help="Run for every symbol in SYMBOLS")
    parser.add_argument("--timeframe", type=str, default="3m", help=f"Timeframe: one of {list(TIMEFRAMES.keys())} (default 3m)")
    parser.add_argument("--all-timeframes", action="store_true", help="Run for every timeframe, not just --timeframe")
    parser.add_argument("--from", dest="from_date", type=str, help="Start date YYYY-MM-DD (for a one-time fetch)")
    parser.add_argument("--to", dest="to_date", type=str, help="End date YYYY-MM-DD (for a one-time fetch, default today)")
    parser.add_argument("--update", action="store_true", help="Incremental update -- only fetch candles since the last saved one")
    parser.add_argument("--verify", action="store_true", help="Verify saved data for gaps/duplicates (no fetching)")
    args = parser.parse_args()

    if not args.symbol and not args.all:
        parser.error("Specify --symbol SYMBOL or --all")

    symbols = list(SYMBOLS.keys()) if args.all else [args.symbol]
    for s in symbols:
        if s not in SYMBOLS:
            parser.error(f"Unknown symbol: {s}. Available: {list(SYMBOLS.keys())}")

    timeframes = list(TIMEFRAMES.keys()) if args.all_timeframes else [args.timeframe]
    for tf in timeframes:
        if tf not in TIMEFRAMES:
            parser.error(f"Unknown timeframe: {tf}. Available: {list(TIMEFRAMES.keys())}")

    print("Logging in to Angel One...")
    angel = AngelOneFetcher()
    if not angel.client:
        print("ERROR: Angel One login failed -- check .env credentials.")
        sys.exit(1)

    engine = HistoryEngine(angel, SYMBOLS)

    for symbol in symbols:
        for tf in timeframes:
            print(f"\n=== {symbol} [{tf}] ===")
            try:
                if args.verify:
                    result = engine.verify_database(symbol, tf)
                elif args.update:
                    result = engine.update_latest(symbol, tf)
                elif args.from_date:
                    to_date = args.to_date or __import__("datetime").date.today().isoformat()
                    result = engine.fetch(symbol, tf, args.from_date, to_date)
                else:
                    print("  Specify --update, --verify, or --from/--to for a one-time fetch.")
                    continue
                for k, v in result.items():
                    print(f"  {k}: {v}")
            except Exception as e:
                print(f"  ERROR: {e}")


if __name__ == "__main__":
    main()
