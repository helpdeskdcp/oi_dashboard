"""
agents/trading_intelligence/data_access.py -- the ONLY module in
agents/trading_intelligence/ that reads live/live-adjacent market data, and
it never touches a broker: every function here is a plain SQLite SELECT
against `cycles`/`strikes`/`market_structure_snapshots` (all owned and
written by app.py's own live loop -- this module never creates or migrates
them) or a read through backtest.py's own candle-archive loader. Same
"read-only seam, zero broker imports" pattern
agents/risk_manager/data_access.py and agents/quant_researcher/data_access.py
already established.

Returns real, reuse-able `oi_engine.StrikeRow` objects for strike rows --
never a redefinition of that dataclass (oi_engine.py's own docstring:
"Never duplicate this logic elsewhere").
"""
import sqlite3

DB_PATH = "oi_history.db"


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def latest_cycle(symbol: str) -> dict | None:
    """The most recent option-chain cycle for `symbol`, with its strikes
    attached -- what "live" means in this dev/CI environment (no broker
    session exists here), and what "live" ALSO means in production (the
    cycle app.py's own loop just wrote): this function has no idea which
    case it's in, and doesn't need to -- it's honest either way. Returns
    None if `symbol` has never had a cycle logged."""
    conn = _connect()
    try:
        cycle = conn.execute(
            "SELECT * FROM cycles WHERE symbol=? ORDER BY ts DESC LIMIT 1", (symbol,)
        ).fetchone()
        if cycle is None:
            return None
        strikes = conn.execute(
            "SELECT * FROM strikes WHERE cycle_id=? ORDER BY strike", (cycle["id"],)
        ).fetchall()
    finally:
        conn.close()
    return {"cycle": dict(cycle), "rows": [_row_to_strike_row(r) for r in strikes]}


def recent_cycles(symbol: str, *, limit: int = 20) -> list:
    """Most recent `limit` cycles (newest first), cycle-only (no strikes) --
    what institutional_intelligence.py's history-based checks (OI-change
    elevation, PCR trend) need: a rolling window of PCR/max_pain/underlying_ltp
    readings, not the full strike ladder each time."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM cycles WHERE symbol=? ORDER BY ts DESC LIMIT ?", (symbol, limit)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def recent_strike_history(symbol: str, strike: int, *, limit: int = 20) -> list:
    """Most recent `limit` readings for ONE strike (newest first), joined
    against its parent cycle's ts -- what OI-change-elevation and
    volume-expansion checks need per-strike (they compare THIS cycle's
    reading against a rolling average of the SAME strike's own recent
    history, never a cross-strike average)."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT s.* FROM strikes s JOIN cycles c ON s.cycle_id = c.id
               WHERE c.symbol=? AND s.strike=? ORDER BY c.ts DESC LIMIT ?""",
            (symbol, strike, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def recent_strike_history_with_underlying(symbol: str, strike: int, *, limit: int = 20) -> list:
    """Same rows as recent_strike_history() (newest first), PLUS the
    parent cycle's own underlying_ltp/ts -- what Trade Guardian's
    empirical premium-vs-underlying sensitivity read needs (comparing
    this strike's own premium against the underlying's own price at the
    SAME cycle, never a fixed/assumed delta). A separate function rather
    than adding columns to recent_strike_history()'s own row shape,
    since that function's existing callers (institutional_intelligence.py)
    already depend on its exact current column set."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT s.*, c.underlying_ltp AS underlying_ltp, c.ts AS cycle_ts
               FROM strikes s JOIN cycles c ON s.cycle_id = c.id
               WHERE c.symbol=? AND s.strike=? ORDER BY c.ts DESC LIMIT ?""",
            (symbol, strike, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def latest_market_structure(symbol: str) -> dict | None:
    """Most recent market_structure_snapshots row for `symbol` -- VWAP,
    ATR, regime, PDH/PDL, and the ALREADY-COMPUTED mother_candle/
    liquidity_sweep JSON blobs app.py's own market_structure.
    build_market_structure() wrote. Reused as-is, never recomputed here."""
    import json

    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM market_structure_snapshots WHERE symbol=? ORDER BY ts DESC LIMIT 1", (symbol,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    d = dict(row)
    for json_field in ("mother_candle_json", "liquidity_sweep_json", "custom_levels_json"):
        d[json_field.replace("_json", "")] = json.loads(d[json_field]) if d.get(json_field) else None
    return d


def recent_market_structure(symbol: str, *, limit: int = 20) -> list:
    """Most recent `limit` market_structure_snapshots readings for
    `symbol` (newest first, index 0 == the same row latest_market_structure()
    would return) -- what regime_profile.py's own volatility-regime read
    needs: a rolling window of atr_14 readings to rank the CURRENT reading
    against, the same "percentile of its own recent history" convention
    strike_intelligence._iv_rank() already established for IV. Does NOT
    decode the JSON blob fields (mother_candle_json etc.) -- callers that
    need those already have latest_market_structure() for the single most
    recent row; this is a lighter, history-only read."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM market_structure_snapshots WHERE symbol=? ORDER BY ts DESC LIMIT ?", (symbol, limit),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _row_to_strike_row(row) -> "StrikeRow":
    """SQLite returns NULL as None for any column never written this cycle
    (e.g. Greeks before the IV/Greeks migration ran, or a strike Angel One
    didn't return quote data for) -- StrikeRow's own dataclass defaults
    (0.0 for every numeric field, "Neutral" for signal fields) are what
    every consumer of a StrikeRow (oi_engine.py's own functions) already
    assumes it can rely on, never None. Coerced explicitly here, the same
    per-field None-to-default pattern backtest.load_cycles() already uses
    for its own StrikeRow construction, generalized across every field
    rather than hand-listed one at a time."""
    from oi_engine import StrikeRow
    d = dict(row)
    defaults = {f: field.default for f, field in StrikeRow.__dataclass_fields__.items()}
    coerced = {k: (v if v is not None else defaults[k]) for k, v in d.items() if k in defaults}
    return StrikeRow(**coerced)


def load_candles(symbol: str, *, timeframe: str = "3m"):
    """Real archived candles -- same accessor
    agents.quant_researcher.data_access.load_candles() already wraps,
    called directly here rather than via that module (this package is
    Milestone 10's own, independent of agents.quant_researcher)."""
    import backtest
    return backtest.load_intraday_candles(symbol, timeframe=timeframe)


def load_fresh_candles(symbol: str, *, timeframe: str = "3m"):
    """load_candles() merged with candle_recorder's in-process,
    continuously-updated candles when there are any -- load_candles()'s
    own archive only updates once a day (fetch_history.py's 18:00 IST
    cron), so intraday callers that reason about TODAY's actual price
    action (structure_alerts.py, structure_overlay.py,
    multi_timeframe.py) use this instead of the plain archive. Every
    other load_candles() caller (shadow_mode, quant_researcher,
    risk_manager, ai_trading_engine's calibration reads) is deliberately
    untouched -- unaffected by this function's existence.

    candle_recorder only ever covers timeframe in {"1m","3m","5m"} (see
    its own TIMEFRAMES_SECONDS) -- any other timeframe falls back to the
    plain archive unchanged. Recorded candles win on any timestamp both
    sources share (the recorder is strictly fresher-or-equal); archive
    history further back than the recorder's own in-memory/DB window is
    kept as-is."""
    import pandas as pd

    from . import candle_recorder

    archived = load_candles(symbol, timeframe=timeframe)
    if timeframe not in candle_recorder.TIMEFRAMES_SECONDS:
        return archived

    recorded = candle_recorder.get_recent_candles(symbol, timeframe)
    if not recorded:
        return archived

    by_time = {row["datetime"]: row for row in archived.to_dict("records")} if not archived.empty else {}
    for row in recorded:
        by_time[row["datetime"]] = row
    merged = sorted(by_time.values(), key=lambda row: row["datetime"])
    return pd.DataFrame(merged)
