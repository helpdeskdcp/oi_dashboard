"""
verify_data_fetching.py -- checks whether data-fetching decisions are
CORRECT right now: for each symbol, is it currently inside/outside market
hours, is DEV_MODE_WHEN_CLOSED affecting it, and does the most recent
database update match what SHOULD be happening given those settings?

Reuses app.py's ACTUAL is_market_open() function (not a reimplementation) --
this guarantees the verifier can never silently drift out of sync with the
real decision logic; if app.py's market-hours logic ever changes, this
script automatically checks against the new behavior too.

USAGE (on VPS, in ~/oi_dashboard):
    ./venv/bin/python3 verify_data_fetching.py
"""
import sqlite3
import datetime as dt
import sys

sys.path.insert(0, ".")
import logging
logging.disable(logging.CRITICAL)   # suppress app.py's startup logging noise

import app as appmod  # reuses the REAL is_market_open, SYMBOLS, DEV_MODE_WHEN_CLOSED, DB_PATH


def main():
    now = appmod.now_ist()
    print(f"Current IST time: {now.strftime('%Y-%m-%d %H:%M:%S')} ({now.strftime('%A')})")
    print(f"DEV_MODE_WHEN_CLOSED: {appmod.DEV_MODE_WHEN_CLOSED}")
    print()

    conn = sqlite3.connect(appmod.DB_PATH)
    conn.row_factory = sqlite3.Row

    print(f"{'Symbol':<12} {'Market':<8} {'Expected Behavior':<28} {'Last DB Update':<20} {'Verdict'}")
    print("-" * 100)

    issues_found = []

    for symbol, cfg in appmod.SYMBOLS.items():
        is_open, reason = appmod.is_market_open(cfg)
        market_str = "OPEN" if is_open else "CLOSED"

        if is_open:
            expected = "Fetch every ~7s (live)"
            max_staleness_min = 2   # if market is open, data should be very fresh
        elif appmod.DEV_MODE_WHEN_CLOSED:
            expected = f"Throttled fetch (~{appmod.DEV_MODE_REFRESH_SECONDS}s), stale OK"
            max_staleness_min = 5
        else:
            expected = "No fetching (sleeping)"
            max_staleness_min = None   # can't judge staleness meaningfully when fetching is intentionally off

        row = conn.execute(
            "SELECT date, time, ts FROM cycles WHERE symbol=? ORDER BY ts DESC LIMIT 1", (symbol,)
        ).fetchone()

        if not row:
            if cfg.get("type") == "index_spot":
                last_update_str = "N/A (spot-only)"
                verdict = "✅ OK (by design -- no option chain, not logged to cycles table)"
            else:
                last_update_str = "NEVER"
                verdict = "⚠️  NO DATA EVER LOGGED"
                issues_found.append(f"{symbol}: no data has ever been logged")
        else:
            last_dt = dt.datetime.fromisoformat(row["ts"]) if "T" in str(row["ts"]) else None
            # ts might be stored as a plain float/unix timestamp depending on schema version -- handle both
            try:
                last_dt = dt.datetime.fromtimestamp(float(row["ts"]))
            except (ValueError, TypeError):
                last_dt = dt.datetime.strptime(f"{row['date']} {row['time']}", "%Y-%m-%d %H:%M:%S")

            staleness_min = (now.replace(tzinfo=None) - last_dt).total_seconds() / 60
            last_update_str = f"{row['date']} {row['time']}"

            if max_staleness_min is None:
                verdict = "✅ OK (fetching intentionally off)"
            elif staleness_min <= max_staleness_min:
                verdict = "✅ OK"
            else:
                verdict = f"⚠️  STALE ({staleness_min:.0f} min old)"
                issues_found.append(f"{symbol}: expected fresh data but last update was {staleness_min:.0f} min ago (market {market_str.lower()})")

        print(f"{symbol:<12} {market_str:<8} {expected:<28} {last_update_str:<20} {verdict}")

    conn.close()

    print()
    print("=" * 60)
    if issues_found:
        print(f"⚠️  {len(issues_found)} POTENTIAL ISSUE(S) FOUND:")
        for issue in issues_found:
            print(f"   - {issue}")
        print()
        print("If market is genuinely open and a symbol is stale, check app_stdout.log")
        print("for errors on that symbol (rate-limits, login failures, etc.)")
    else:
        print("✅ ALL DATA-FETCHING DECISIONS LOOK CORRECT for current market hours + dev-mode setting.")
    print("=" * 60)


if __name__ == "__main__":
    main()
