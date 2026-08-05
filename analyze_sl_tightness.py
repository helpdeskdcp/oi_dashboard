"""
analyze_sl_tightness.py -- For every genuine STOP LOSS paper-trade, checks
what the underlying price did AFTER the exit -- did it move favorably
(suggesting the SL was too tight / premature) or continue unfavorably
(suggesting the SL was appropriately placed)?

HONEST LIMITATION: this checks the UNDERLYING's movement, not the option
PREMIUM's (which we don't have a clean historical replay for outside of
paper_trades themselves). Underlying-direction is a reasonable PROXY --
if the underlying kept moving favorably, the premium almost certainly
would have too -- but it's not a perfect substitute for replaying the
actual premium series.

USAGE:
    ./venv/bin/python3 analyze_sl_tightness.py --days 30
"""
import argparse
import datetime as dt
import sqlite3

DB_PATH = "oi_history.db"


def analyze(days):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    trades = conn.execute(
        """SELECT * FROM paper_trades
           WHERE exit_reason='STOP LOSS' AND entry_ts >= strftime('%s','now',?)
           ORDER BY entry_ts""",
        (f"-{days} days",),
    ).fetchall()

    print(f"Analyzing {len(trades)} STOP-LOSS trades from the last {days} days...\n")

    would_have_helped = 0
    would_not_have_helped = 0
    no_data = 0

    for t in trades:
        # BUG FIX: cycles.ts is stored as an ISO-8601 STRING (e.g.
        # "2026-07-30T19:21:05.678139"), NOT a numeric Unix-epoch like
        # paper_trades.entry_ts. Comparing a REAL against a TEXT column
        # silently matched nothing. Convert entry_ts to the SAME ISO-string
        # format for a genuinely correct (lexicographic) comparison.
        entry_dt = dt.datetime.fromtimestamp(t["entry_ts"])
        window_end_dt = entry_dt + dt.timedelta(minutes=30)
        entry_iso = entry_dt.isoformat()
        window_end_iso = window_end_dt.isoformat()

        cycles_after = conn.execute(
            """SELECT underlying_ltp FROM cycles WHERE symbol=? AND ts BETWEEN ? AND ?
               ORDER BY ts ASC""",
            (t["symbol"], entry_iso, window_end_iso),
        ).fetchall()

        if len(cycles_after) < 3:
            no_data += 1
            continue

        prices = [c["underlying_ltp"] for c in cycles_after if c["underlying_ltp"] is not None]
        if len(prices) < 3:
            no_data += 1
            continue

        entry_underlying = prices[0]
        later_underlying = prices[-1]
        move_pct = (later_underlying - entry_underlying) / entry_underlying * 100

        # Direction the trade needed: CE wants underlying UP, PE wants underlying DOWN
        needed_direction = 1 if t["direction"] == "CE" else -1
        genuinely_favorable = (move_pct * needed_direction) > 0.1   # genuinely moved the needed way afterward

        status = "WOULD-HAVE-HELPED (price moved favorably after SL)" if genuinely_favorable else "SL-WAS-APPROPRIATE (price continued against the trade)"
        if genuinely_favorable:
            would_have_helped += 1
        else:
            would_not_have_helped += 1

        print(f"{t['symbol']:12} {t['direction']:3} entry={t['entry_price']:>8.2f} points={t['points']:>7.2f} "
              f"| underlying-move-after: {move_pct:+.2f}% | {status}")

    print(f"\n--- SUMMARY ---")
    print(f"Would genuinely have helped (SL too tight): {would_have_helped}")
    print(f"SL was genuinely appropriate (market continued against): {would_not_have_helped}")
    print(f"No genuine data to judge: {no_data}")
    if would_have_helped + would_not_have_helped > 0:
        pct = would_have_helped / (would_have_helped + would_not_have_helped) * 100
        print(f"\n{pct:.0f}% of judgeable STOP-LOSS trades show the underlying moved favorably afterward.")
        print("(This is a PROXY using underlying-direction, not a guarantee the option premium")
        print(" would have hit target -- use as a directional signal for further investigation, not a final verdict.)")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    analyze(args.days)
