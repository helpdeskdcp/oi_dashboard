import sqlite3, datetime as dt

conn = sqlite3.connect('oi_history.db')
conn.row_factory = sqlite3.Row
today = dt.date.today().isoformat()

rows = conn.execute(
    "SELECT time, underlying_ltp, atm, pcr, bias, signal_action, signal_confidence, signal_tradeable, note "
    "FROM cycles WHERE symbol='NIFTY' AND date=? ORDER BY ts ASC", (today,)
).fetchall()

print(f"Total NIFTY cycles today: {len(rows)}\n")

last_bias = None
for r in rows:
    if r['bias'] != last_bias:
        tradeable = "TRADEABLE" if r['signal_tradeable'] else "not-tradeable"
        print(f"{r['time']} | LTP={r['underlying_ltp']} ATM={r['atm']} PCR={r['pcr']} | "
              f"Bias={r['bias']} | Signal={r['signal_action']} Conf={r['signal_confidence']} ({tradeable})")
        last_bias = r['bias']

print(f"\n--- Highest confidence seen today ---")
best = max(rows, key=lambda r: r['signal_confidence'] or 0, default=None)
if best:
    print(f"{best['time']} | Conf={best['signal_confidence']} | Bias={best['bias']} | Tradeable={bool(best['signal_tradeable'])}")
    print(f"Note: {best['note']}")
