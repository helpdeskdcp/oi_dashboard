"""
agents/intelligence_alerts/store.py -- Milestone 14, Phase 1: this
package's own isolated table namespace (intelligence_alerts_log).
CREATE TABLE IF NOT EXISTS only, no migration of any existing table, no
write anywhere else in this database -- same DB_PATH/_connect()/init_db()
shape agents/intelligence_history/store.py already established.

One row per triggered rule (append-only -- nothing here is ever
UPDATEd, only INSERTed). delivered_telegram/delivered_email record
whether a send was ATTEMPTED (the channel was configured and the call
didn't raise) -- app.py's own send_telegram() doesn't return a delivery
confirmation, so these are honestly "attempted", not "confirmed
received".
"""
import sqlite3

DB_PATH = "oi_history.db"


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
            CREATE TABLE IF NOT EXISTS intelligence_alerts_log (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                  TEXT NOT NULL,
                symbol              TEXT NOT NULL,
                rule                TEXT NOT NULL,
                detail              TEXT NOT NULL,
                delivered_telegram  INTEGER NOT NULL DEFAULT 0,
                delivered_email     INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_intelligence_alerts_log_symbol_ts
                ON intelligence_alerts_log(symbol, ts);
            """
        )
        conn.commit()
    finally:
        conn.close()


def record_alert(*, ts, symbol, rule, detail, delivered_telegram=False, delivered_email=False) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO intelligence_alerts_log "
            "(ts, symbol, rule, detail, delivered_telegram, delivered_email) "
            "VALUES (?,?,?,?,?,?)",
            (ts, symbol, rule, detail, int(delivered_telegram), int(delivered_email)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_recent(*, symbol: str | None = None, limit: int = 10, offset: int = 0) -> list:
    """Newest-first logged alerts -- api.get_recent()'s own data source."""
    conn = _connect()
    try:
        sql = "SELECT * FROM intelligence_alerts_log "
        params: list = []
        if symbol:
            sql += "WHERE symbol = ? "
            params.append(symbol)
        sql += "ORDER BY ts DESC LIMIT ? OFFSET ?"
        params.append(limit)
        params.append(offset)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def count_total(symbol: str | None = None) -> int:
    conn = _connect()
    try:
        if symbol:
            return conn.execute(
                "SELECT COUNT(*) FROM intelligence_alerts_log WHERE symbol = ?", (symbol,)
            ).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM intelligence_alerts_log").fetchone()[0]
    finally:
        conn.close()


def last_alert_ts(symbol: str | None = None) -> str | None:
    conn = _connect()
    try:
        if symbol:
            row = conn.execute(
                "SELECT ts FROM intelligence_alerts_log WHERE symbol = ? ORDER BY ts DESC LIMIT 1", (symbol,)
            ).fetchone()
        else:
            row = conn.execute("SELECT ts FROM intelligence_alerts_log ORDER BY ts DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return row["ts"] if row else None


def last_alert_ts_for_rule(*, symbol: str, rule: str) -> str | None:
    """Milestone 14, Phase 2: cooldown lookup -- the automatic
    background-loop evaluator uses this to avoid re-alerting the same
    (symbol, rule) pair every single cycle while a condition stays
    true."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT ts FROM intelligence_alerts_log WHERE symbol = ? AND rule = ? ORDER BY ts DESC LIMIT 1",
            (symbol, rule),
        ).fetchone()
    finally:
        conn.close()
    return row["ts"] if row else None
