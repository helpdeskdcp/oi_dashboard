"""
agents/trading_intelligence/candle_recorder.py -- Milestone 20, Phase 6:
real 1m/3m/5m candles built in-process from the LTP ticks app.py's own
run_symbol_loop() already fetches every cycle (REFRESH_INTERVAL=1s for
the actively-viewed symbol, BACKGROUND_REFRESH_SECONDS=45s for the
rest) -- ZERO new broker calls, ZERO new Angel One logins. Nothing here
ever imports app.py or opens a broker session; app.py's own loop calls
append_tick() into this module (the same "app.py calls INTO
agents.trading_intelligence, never the other way around" direction
every other integration point in this package already uses), and this
module owns its own in-memory state entirely independently.

Root cause this fixes: data_access.load_candles()'s archive
(data/history/<symbol>/3m.parquet) is only refreshed once a day by
fetch_history.py's 18:00 IST cron -- see that script's own docstring.
Every structure_alerts.py/structure_overlay.py/multi_timeframe.py read
of "the candles" was therefore reasoning about YESTERDAY's data for the
entire trading day, which is why the exact same stale breakout/retest
pair (and therefore the exact same trade-plan overlay) kept getting
re-detected and re-alerted for hours -- the input never changed until
18:00. This module gives those callers real, continuously-updated 1m/
3m/5m bars instead, via data_access.load_fresh_candles().

Persistence: every CLOSED bucket is written through to the `live_candles`
SQLite table (oi_history.db) immediately -- "archive" in the sense
structure_alerts.py/multi_timeframe.py need (continuously updated,
readable across a restart), NOT the once-daily parquet file
history_engine.py owns (never touched here). In-memory is the hot path
(get_recent_candles() reads it first); the DB is the cold-start
fallback for a fresh process. A DB write failing is logged and
swallowed -- the in-memory candle (what every live-cycle caller
actually reads) must never be lost over a transient disk/lock issue,
same "an observability write failing must never defeat the real
action" contract used throughout this package.
"""
import collections
import datetime as dt
import logging
import sqlite3
import threading

from .. import timekeeping

log = logging.getLogger(__name__)

DB_PATH = "oi_history.db"

TIMEFRAMES_SECONDS = {"1m": 60, "3m": 180, "5m": 300}
MAX_CANDLES_IN_MEMORY = 1000

_lock = threading.Lock()
_completed = collections.defaultdict(lambda: collections.deque(maxlen=MAX_CANDLES_IN_MEMORY))  # {(symbol, tf): deque[candle]}
_forming = {}  # {(symbol, tf): {"start": dt, "open":, "high":, "low":, "close":, "volume": int}}


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    """Idempotent -- same CREATE TABLE IF NOT EXISTS convention every
    other *_store.py/data_access.py module in this project uses."""
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS live_candles (
                symbol TEXT NOT NULL, timeframe TEXT NOT NULL, datetime TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL, volume INTEGER,
                PRIMARY KEY (symbol, timeframe, datetime)
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_live_candles_symbol_tf ON live_candles(symbol, timeframe, datetime)")
        conn.commit()
    finally:
        conn.close()


def _persist_candle(symbol: str, timeframe: str, candle: dict) -> None:
    try:
        conn = _connect()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO live_candles (symbol, timeframe, datetime, open, high, low, close, volume) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (symbol, timeframe, candle["datetime"].isoformat(), candle["open"], candle["high"],
                 candle["low"], candle["close"], candle["volume"]),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"[CANDLE_RECORDER] persist failed SYMBOL={symbol} TF={timeframe}: {e}")


def _bucket_start(ts: dt.datetime, seconds: int) -> dt.datetime:
    epoch = ts.timestamp()
    bucket = epoch - (epoch % seconds)
    return dt.datetime.fromtimestamp(bucket)


def append_tick(symbol: str, ts: dt.datetime, ltp: float, *, volume: int = 0) -> None:
    """Feeds one live LTP tick into every tracked timeframe's
    in-progress bucket, closing (persisting + appending to the
    in-memory deque) any bucket the new tick has moved past. Never
    raises -- a malformed tick is logged and dropped, and this is
    exactly the kind of best-effort call app.py's run_symbol_loop()
    wraps in its own try/except so a bug here can never break the real
    live cycle."""
    if ltp is None or ltp <= 0:
        return
    with _lock:
        for tf, seconds in TIMEFRAMES_SECONDS.items():
            key = (symbol, tf)
            bucket_start = _bucket_start(ts, seconds)
            current = _forming.get(key)
            if current is None or current["start"] != bucket_start:
                if current is not None:
                    closed = {
                        "datetime": current["start"], "open": current["open"], "high": current["high"],
                        "low": current["low"], "close": current["close"], "volume": current["volume"],
                    }
                    _completed[key].append(closed)
                    log.info(f"[CANDLE_RECORDER] SYMBOL={symbol} TF={tf} CLOSED datetime={closed['datetime']} "
                             f"o={closed['open']} h={closed['high']} l={closed['low']} c={closed['close']}")
                    _persist_candle(symbol, tf, closed)
                _forming[key] = {"start": bucket_start, "open": ltp, "high": ltp, "low": ltp, "close": ltp, "volume": volume}
            else:
                current["high"] = max(current["high"], ltp)
                current["low"] = min(current["low"], ltp)
                current["close"] = ltp
                current["volume"] += volume


def _load_from_db(symbol: str, timeframe: str, limit: int) -> list:
    try:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT datetime, open, high, low, close, volume FROM live_candles "
                "WHERE symbol=? AND timeframe=? ORDER BY datetime DESC LIMIT ?",
                (symbol, timeframe, limit),
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        log.warning(f"[CANDLE_RECORDER] DB read failed SYMBOL={symbol} TF={timeframe}: {e}")
        return []
    candles = [
        {"datetime": dt.datetime.fromisoformat(r["datetime"]), "open": r["open"], "high": r["high"],
         "low": r["low"], "close": r["close"], "volume": r["volume"]}
        for r in rows
    ]
    return list(reversed(candles))


def get_recent_candles(symbol: str, timeframe: str, limit: int = 500) -> list:
    """Completed candles only (the in-progress bucket is deliberately
    excluded -- a still-forming bar's high/low/close are not final).
    In-memory deque is the hot path (this process's own recorded
    ticks); falls back to the live_candles DB table for a cold-started
    process (e.g. a freshly restarted app, or a CLI/analytics process
    reading what the live app already persisted)."""
    with _lock:
        candles = list(_completed.get((symbol, timeframe), ()))
    if candles:
        return candles[-limit:]
    return _load_from_db(symbol, timeframe, limit)


def last_candle_time(symbol: str, timeframe: str) -> dt.datetime | None:
    candles = get_recent_candles(symbol, timeframe, limit=1)
    return candles[-1]["datetime"] if candles else None


def candle_lag_seconds(symbol: str, timeframe: str, *, now: dt.datetime | None = None) -> float | None:
    """Seconds between `now` and the last completed candle -- the
    freshness health metric GET /api/runtime/candle-freshness surfaces.
    None (not a fabricated number) when there's no recorded candle yet.
    Milestone 25: defaults to timekeeping.now_ist(), matching the clock
    every caller of append_tick() (app.py's run_symbol_loop()) now
    stamps ts with -- a mismatched clock here would silently misreport
    freshness."""
    last = last_candle_time(symbol, timeframe)
    if last is None:
        return None
    now = now or timekeeping.now_ist()
    return round((now - last).total_seconds(), 1)


def reconcile_from_broker_candles(symbol: str, timeframe: str, broker_candles: list) -> int:
    """Production hardening (candle-gap recovery): heals whatever this
    symbol's live_candles history is missing -- a VPS reboot, a crash-
    restart, any downtime -- using ALREADY-FETCHED broker candles
    (`broker_candles`), never a new broker call of its own. The one
    real caller (app.py's run_symbol_loop) already fetches 5 days of
    real THREE_MINUTE candles once a day for market-structure/Ichimoku
    purposes; this just also feeds that same data through here, so a
    fresh process picks up wherever its own ticks left off, not a blank
    slate that only starts filling in from the moment it restarted.

    Each broker candle is a real, COMPLETE bar (not built incrementally
    from ticks like append_tick()'s own in-progress buckets), so it's
    written straight through via _persist_candle() -- the same
    (symbol, timeframe, datetime) PRIMARY KEY that already makes writes
    idempotent handles any overlap with what's already recorded, no
    manual gap-detection needed. If a live in-progress bucket
    (_forming) covers the SAME period as a broker candle, that forming
    entry is dropped -- the broker's complete bar supersedes it, and
    without this a later tick would eventually close the stale forming
    bucket too, appending a duplicate. Returns how many candles were
    written (for logging -- not a promise that all of them were
    previously missing, since re-confirming already-known candles is
    harmless and counted the same way)."""
    if not broker_candles:
        return 0
    with _lock:
        merged = {c["datetime"]: c for c in _completed.get((symbol, timeframe), ())}
        for row in broker_candles:
            candle = {"datetime": row["datetime"], "open": row["open"], "high": row["high"],
                      "low": row["low"], "close": row["close"], "volume": row.get("volume", 0) or 0}
            merged[candle["datetime"]] = candle
        ordered = sorted(merged.values(), key=lambda c: c["datetime"])

        forming = _forming.get((symbol, timeframe))
        if forming is not None and forming["start"] in merged:
            del _forming[(symbol, timeframe)]
        _completed[(symbol, timeframe)] = collections.deque(ordered[-MAX_CANDLES_IN_MEMORY:], maxlen=MAX_CANDLES_IN_MEMORY)

    for row in broker_candles:
        _persist_candle(symbol, timeframe, {"datetime": row["datetime"], "open": row["open"], "high": row["high"],
                                             "low": row["low"], "close": row["close"], "volume": row.get("volume", 0) or 0})
    log.info(f"[CANDLE_RECORDER] SYMBOL={symbol} TF={timeframe} RECONCILED {len(broker_candles)} candles from broker fetch")
    return len(broker_candles)
