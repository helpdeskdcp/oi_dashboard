"""
agents/intelligence_history/store.py -- Milestone 13, Phase 2: this
package's own isolated table namespace (intelligence_snapshots_log).
CREATE TABLE IF NOT EXISTS only, no migration of any existing table, no
write anywhere else in this database -- mirrors the exact
DB_PATH/_connect()/init_db() shape agents/shadow_mode/store.py already
established.

One table, one row per logged snapshot (append-only -- nothing here is
ever UPDATEd, only INSERTed): intelligence_snapshots_log records the full
MarketIntelligenceSnapshot (intelligence_models.py) at one point in time
for one symbol, so analytics.py has a real history to compute drift/
stability statistics from. Recording a snapshot NEVER touches any other
table -- there is no execution path from this table to any order/trade
table.
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
            CREATE TABLE IF NOT EXISTS intelligence_snapshots_log (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                  TEXT NOT NULL,
                symbol              TEXT NOT NULL,
                timeframe           TEXT NOT NULL,
                bias                TEXT NOT NULL,
                confidence          INTEGER,
                oi_strength         INTEGER,
                probability_score   INTEGER,
                volume_score        INTEGER,
                greeks_alignment    TEXT,
                institutional_score INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_intelligence_snapshots_log_symbol_ts
                ON intelligence_snapshots_log(symbol, ts);
            """
        )
        conn.commit()
    finally:
        conn.close()


def record_snapshot(*, ts, symbol, timeframe, snapshot) -> int:
    """`snapshot` is an intelligence_models.MarketIntelligenceSnapshot
    (or anything with the same to_dict() shape) -- every column below
    comes straight from it, no re-derivation."""
    data = snapshot.to_dict() if hasattr(snapshot, "to_dict") else dict(snapshot)
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO intelligence_snapshots_log "
            "(ts, symbol, timeframe, bias, confidence, oi_strength, probability_score, "
            " volume_score, greeks_alignment, institutional_score) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, symbol, timeframe, data["bias"], data["confidence"], data["oi_strength"],
             data["probability_score"], data["volume_score"], data["greeks_alignment"],
             data["institutional_score"]),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_recent(*, symbol: str | None = None, limit: int = 10) -> list:
    """Newest-first logged snapshots -- api.get_recent()'s own data
    source."""
    conn = _connect()
    try:
        sql = "SELECT * FROM intelligence_snapshots_log "
        params: list = []
        if symbol:
            sql += "WHERE symbol = ? "
            params.append(symbol)
        sql += "ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def list_since(*, symbol: str, since_ts: str | None = None) -> list:
    """Oldest-first logged snapshots for one symbol -- analytics.py's own
    data source (chronological order is required for consecutive-
    transition/delta calculations)."""
    conn = _connect()
    try:
        sql = "SELECT * FROM intelligence_snapshots_log WHERE symbol = ? "
        params: list = [symbol]
        if since_ts:
            sql += "AND ts >= ? "
            params.append(since_ts)
        sql += "ORDER BY ts ASC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def count_total() -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM intelligence_snapshots_log").fetchone()[0]
    finally:
        conn.close()


def count_since(since_ts: str) -> int:
    conn = _connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM intelligence_snapshots_log WHERE ts >= ?", (since_ts,)
        ).fetchone()[0]
    finally:
        conn.close()


def last_snapshot_ts(symbol: str | None = None) -> str | None:
    conn = _connect()
    try:
        if symbol:
            row = conn.execute(
                "SELECT ts FROM intelligence_snapshots_log WHERE symbol = ? ORDER BY ts DESC LIMIT 1", (symbol,)
            ).fetchone()
        else:
            row = conn.execute("SELECT ts FROM intelligence_snapshots_log ORDER BY ts DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return row["ts"] if row else None
