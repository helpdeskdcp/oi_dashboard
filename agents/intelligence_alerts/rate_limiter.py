"""
agents/intelligence_alerts/rate_limiter.py -- Milestone 15, Phase 2:
Alert Rate Limiting. SQLite-backed rolling 1-hour window, own isolated
table (intelligence_alert_send_log), CREATE TABLE IF NOT EXISTS only --
same shape every other store.py in this package already uses. One row
per actual SEND (recorded by the caller via record_send(), never
implied by a check), not per evaluation -- a distinct concern from
dedup_store.py's own state table, which tracks "is this the SAME
condition repeating," not raw volume.

is_allowed() is this module's one real decision: given a symbol and the
current global/per-symbol counts in the last rolling hour (a send stops
counting exactly 1 hour after it happened -- not a calendar-hour
bucket), would sending one more alert stay within both limits?
"""
import datetime as dt
import logging
import sqlite3

DB_PATH = "oi_history.db"
log = logging.getLogger("intelligence_alerts.rate_limiter")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS intelligence_alert_send_log (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                ts      TEXT NOT NULL,
                symbol  TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_intelligence_alert_send_log_ts
                ON intelligence_alert_send_log(ts);
            CREATE INDEX IF NOT EXISTS idx_intelligence_alert_send_log_symbol_ts
                ON intelligence_alert_send_log(symbol, ts);

            CREATE TABLE IF NOT EXISTS intelligence_alert_rate_limit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT NOT NULL,
                symbol      TEXT NOT NULL,
                limit_type  TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def count_rate_limited(since_iso: str | None = None) -> int:
    """Milestone 15, Phase 3: Runtime Scheduler Observability's own
    "alerts_rate_limited" figure -- DB-backed for the same test-
    isolation reason dedup_store.count_suppressions() is."""
    conn = _connect()
    try:
        if since_iso:
            return conn.execute(
                "SELECT COUNT(*) FROM intelligence_alert_rate_limit_log WHERE ts >= ?", (since_iso,)
            ).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM intelligence_alert_rate_limit_log").fetchone()[0]
    finally:
        conn.close()


def _record_rate_limit_hit(symbol: str, limit_type: str, now: dt.datetime) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO intelligence_alert_rate_limit_log (ts, symbol, limit_type) VALUES (?,?,?)",
            (now.isoformat(), symbol, limit_type),
        )
        conn.commit()
    finally:
        conn.close()


def record_send(symbol: str, now=None) -> None:
    now = now or dt.datetime.now()
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO intelligence_alert_send_log (ts, symbol) VALUES (?, ?)", (now.isoformat(), symbol),
        )
        conn.commit()
    finally:
        conn.close()


def _count_since(symbol, since_iso: str) -> int:
    conn = _connect()
    try:
        if symbol is None:
            return conn.execute(
                "SELECT COUNT(*) FROM intelligence_alert_send_log WHERE ts >= ?", (since_iso,)
            ).fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM intelligence_alert_send_log WHERE symbol = ? AND ts >= ?", (symbol, since_iso)
        ).fetchone()[0]
    finally:
        conn.close()


def is_allowed(*, symbol: str, max_per_symbol_per_hour: int, max_total_per_hour: int, now=None) -> bool:
    """True if sending ONE MORE alert for `symbol` right now would stay
    within BOTH limits. Logs RATE_LIMIT_HIT (per-symbol) or
    GLOBAL_RATE_LIMIT_HIT (global) when denying a send -- the per-symbol
    limit is checked first since it's the more specific one, so at most
    one of the two is ever logged for a given call."""
    now = now or dt.datetime.now()
    since_iso = (now - dt.timedelta(hours=1)).isoformat()

    symbol_count = _count_since(symbol, since_iso)
    if symbol_count >= max_per_symbol_per_hour:
        _record_rate_limit_hit(symbol, "symbol", now)
        log.info(f"RATE_LIMIT_HIT symbol={symbol!r} count={symbol_count} limit={max_per_symbol_per_hour}")
        return False

    total_count = _count_since(None, since_iso)
    if total_count >= max_total_per_hour:
        _record_rate_limit_hit(symbol, "global", now)
        log.info(f"GLOBAL_RATE_LIMIT_HIT count={total_count} limit={max_total_per_hour}")
        return False

    return True
