"""
explore_angelone_data.py -- Discovers what data Angel One's SmartAPI
genuinely provides, beyond what we currently use (getCandleData for OHLCV).

READ-ONLY exploration -- makes a few API calls and prints the RAW responses
so we can see exactly what fields are available, rather than assuming.

Key things being tested:
  1. optionGreek  -- Angel One MAY provide Delta/Gamma/Theta/Vega DIRECTLY,
                      which would let us skip months of IV-data-accumulation
                      (see project notes on why we didn't build Greeks
                      analysis yet -- if this genuinely works, that changes).
  2. getOIData    -- a DEDICATED historical-OI endpoint, separate from candles.
  3. putCallRatio -- a direct PCR endpoint.
  4. getCandleData -- already known-working (OHLCV), included for comparison.

USAGE:
    ./venv/bin/python3 explore_angelone_data.py --symbol NIFTY
"""
import argparse
import json
import sys
sys.path.insert(0, ".")

from app import AngelOneFetcher, SYMBOLS


def pretty(label, data):
    print(f"\n{'='*70}\n{label}\n{'='*70}")
    try:
        print(json.dumps(data, indent=2, default=str)[:2000])   # cap output length
    except Exception:
        print(data)


def find_nearest_expiry_ddmmmyyyy(angel, symbol, cfg):
    """Looks up the symbol's nearest expiry from the cached instrument
    master and formats it as DDMMMYYYY (e.g. '25JAN2024'), which is the
    exact format Angel One's optionGreek endpoint requires (confirmed via
    their own SmartAPI forum -- NOT the same format used elsewhere)."""
    import datetime as dt
    master = angel.instruments if hasattr(angel, "instruments") else None
    if not master:
        return None
    candidates = [
        row for row in master
        if row.get("name") == symbol and row.get("instrumenttype") in ("OPTIDX", "OPTSTK", "OPTFUT")
    ]
    if not candidates:
        return None
    expiries = sorted({row["expiry"] for row in candidates if row.get("expiry")})
    if not expiries:
        return None
    # Angel's own expiry-string in the instrument master is usually already
    # close to DDMMMYYYY (e.g. "28JUL2026") -- reuse it directly if so.
    return expiries[0]


def explore(symbol):
    if symbol not in SYMBOLS:
        print(f"Unknown symbol: {symbol}. Available: {list(SYMBOLS.keys())}")
        return

    cfg = SYMBOLS[symbol]
    angel = AngelOneFetcher()
    angel._ensure_session_fresh()
    if not angel.client:
        print("Could not establish Angel One session -- check credentials in .env.")
        return

    print(f"Session established. Exploring data for {symbol}...")
    print("NOTE: Angel One's own forum states optionGreek/getOIData are only reliably")
    print("available for LIVE contracts DURING MARKET HOURS -- results may differ if")
    print("run when the market is closed.\n")

    expiry_str = find_nearest_expiry_ddmmmyyyy(angel, symbol, cfg)
    print(f"Using expiry date: {expiry_str or '(could not determine -- optionGreek will likely fail)'}")

    # --- 1. optionGreek: the potentially-huge discovery ---
    try:
        resp = angel._call_with_relogin(
            angel.client.optionGreek,
            {"name": symbol, "expirydate": expiry_str or ""}
        )
        pretty("optionGreek() response", resp)
    except Exception as e:
        pretty("optionGreek() FAILED", str(e))

    # --- 2. getOIData: dedicated historical-OI endpoint (FIXED format --
    # confirmed via smartapi-python source: identical param-shape to
    # getCandleData, NOT the format tried earlier) ---
    try:
        master = angel.instruments if hasattr(angel, "instruments") else []
        oi_candidates = [
            row for row in master
            if row.get("name") == symbol and row.get("instrumenttype") in ("OPTIDX", "OPTSTK", "OPTFUT", "FUTIDX", "FUTSTK")
        ]
        sample_token = oi_candidates[0]["token"] if oi_candidates else ""
        import datetime as _dt
        to_date = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        from_date = (_dt.datetime.now() - _dt.timedelta(days=5)).strftime("%Y-%m-%d %H:%M")
        resp = angel._call_with_relogin(
            angel.client.getOIData,
            {"exchange": "NFO", "symboltoken": sample_token, "interval": "ONE_DAY", "fromdate": from_date, "todate": to_date}
        )
        pretty("getOIData() response (fixed format)", resp)
    except Exception as e:
        pretty("getOIData() FAILED", str(e))

    # --- 4. NEWLY DISCOVERED: oIBuildup -- native OI-buildup classification ---
    try:
        resp = angel._call_with_relogin(
            angel.client.oIBuildup,
            {"expirytype": "NEAR", "datatype": "Long Built Up"}
        )
        pretty("oIBuildup() response (NEW discovery)", resp)
    except Exception as e:
        pretty("oIBuildup() FAILED", str(e))

    # --- 3. putCallRatio: direct PCR endpoint ---
    try:
        resp = angel._call_with_relogin(angel.client.putCallRatio)
        pretty("putCallRatio() response (first part)", resp)
    except Exception as e:
        pretty("putCallRatio() FAILED", str(e))

    print(f"\n{'='*70}")
    print("Exploration complete. Review the responses above to see which fields")
    print("are genuinely available. If optionGreek() worked and returned real")
    print("delta/gamma/theta/vega values, that's a significant finding -- it")
    print("could let us build Greeks-based analysis MUCH sooner than waiting")
    print("for months of self-collected IV data.")
    print(f"{'='*70}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Explore what data Angel One SmartAPI provides")
    parser.add_argument("--symbol", type=str, required=True)
    args = parser.parse_args()
    explore(args.symbol.upper())
