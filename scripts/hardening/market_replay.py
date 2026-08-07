"""
scripts/hardening/market_replay.py -- Production Hardening Sprint:
"30-day market replay testing."

Runs backtest.simulate_ichimoku_trades (the SAME live code path
app.py's real Ichimoku engine uses -- see that function's own
docstring, "SAME CODE PATH AS LIVE") over the most recent 30 CALENDAR
days present in each symbol's real archived candle data
(data/history/<symbol>/3m.csv, written by history_engine.py). This is
the only engine in backtest.py that runs from candle data alone --
every option-chain engine (V3/SR/Dynamic SR) needs a live oi_history.db
`cycles` table this dev/CI environment doesn't have (see
PRODUCTION_HARDENING_SPRINT.md's "what could and couldn't run in this
environment" section) -- so this script honestly replays what it CAN
run for real, and documents what it can't rather than fabricating
numbers for the option-chain engines.

Usage: python3 scripts/hardening/market_replay.py
Writes hardening_results/market_replay.json (real numbers, re-run to
reproduce/update).
"""
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import backtest

SYMBOLS = ("NIFTY", "BANKNIFTY", "SENSEX", "FINNIFTY", "MIDCPNIFTY", "CRUDEOIL", "GOLD", "SILVER")
REPLAY_DAYS = 30


def _latest_available_date(symbol: str):
    candles = backtest.load_intraday_candles(symbol)
    if candles.empty:
        return None
    return candles["datetime"].max()


def replay_symbol(symbol: str) -> dict:
    latest = _latest_available_date(symbol)
    if latest is None:
        return {"symbol": symbol, "error": "no candle archive available"}

    date_to = latest.date()
    date_from = date_to - dt.timedelta(days=REPLAY_DAYS)
    trades, candles_seen, meta = backtest.simulate_ichimoku_trades(
        symbol, date_from.isoformat(), date_to.isoformat(),
    )
    if meta.get("error"):
        return {"symbol": symbol, "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
                "error": meta["error"]}

    stats = backtest.compute_ichimoku_accuracy_stats(trades)
    return {
        "symbol": symbol, "date_from": date_from.isoformat(), "date_to": date_to.isoformat(),
        "candles_replayed": candles_seen, "trade_count": len(trades),
        "elapsed_seconds": meta["elapsed_seconds"], "stats": stats,
    }


def main():
    results = [replay_symbol(s) for s in SYMBOLS]
    out = {
        "generated_at": dt.datetime.now().isoformat(),
        "engine": "ichimoku_engine (same live code path as app.py -- see simulate_ichimoku_trades docstring)",
        "replay_window_days": REPLAY_DAYS,
        "note": (
            "Option-chain engines (V3/SR/Dynamic SR/V2) were NOT replayed here -- they read "
            "backtest.load_cycles()/load_market_structure_snapshots(), which need a live "
            "oi_history.db `cycles` table this environment does not have (no live broker "
            "session was ever started -- see this repo's own landmine note about triggering "
            "a real duplicate Angel One login). This script replays exactly what CAN be run "
            "for real from the archived candle data alone -- see PRODUCTION_HARDENING_SPRINT.md."
        ),
        "results": results,
    }
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "hardening_results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "market_replay.json")
    with open(out_path, "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(json.dumps(out, indent=2, default=str))
    print(f"\nWritten to {out_path}")


if __name__ == "__main__":
    main()
