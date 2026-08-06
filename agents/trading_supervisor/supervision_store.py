"""
agents/trading_supervisor/supervision_store.py -- "Keep a complete
supervision log for auditing." SQLite persistence for every supervision
decision (supervision_log) and periodic per-agent health snapshot
(agent_health_snapshots). Same module-level DB_PATH + per-call
connect/close + PRAGMA busy_timeout convention agents/audit_log.py,
agents/event_bus.py, and agents/risk_manager/risk_store.py already
established, indexed from the start.
"""
import datetime as dt
import json
import sqlite3

DB_PATH = "oi_history.db"


def _now() -> str:
    return dt.datetime.now().isoformat()


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    """Idempotent -- CREATE TABLE IF NOT EXISTS, safe on every process start."""
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS supervision_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              TEXT NOT NULL,
                candidate_name  TEXT,
                symbol          TEXT,
                decision        TEXT NOT NULL,
                report_json     TEXT NOT NULL,
                audit_log_id    INTEGER
            );

            CREATE TABLE IF NOT EXISTS agent_health_snapshots (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ts             TEXT NOT NULL,
                agent          TEXT NOT NULL,
                is_stale       INTEGER NOT NULL DEFAULT 0,
                is_failing     INTEGER NOT NULL DEFAULT 0,
                snapshot_json  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_supervision_log_symbol_ts ON supervision_log(symbol, ts);
            CREATE INDEX IF NOT EXISTS idx_supervision_log_decision ON supervision_log(decision);
            CREATE INDEX IF NOT EXISTS idx_agent_health_snapshots_agent_ts ON agent_health_snapshots(agent, ts);
            """
        )
        conn.commit()
    finally:
        conn.close()


def record_supervision(report, *, candidate_name, symbol, audit_log_id=None) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO supervision_log (ts, candidate_name, symbol, decision, report_json, audit_log_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), candidate_name, symbol, report.decision, report.to_json(), audit_log_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def record_agent_health(agent, *, is_stale, is_failing, snapshot: dict) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO agent_health_snapshots (ts, agent, is_stale, is_failing, snapshot_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (_now(), agent, 1 if is_stale else 0, 1 if is_failing else 0, json.dumps(snapshot, default=str)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _rows_to_dicts(rows, json_field) -> list:
    out = []
    for r in rows:
        d = dict(r)
        d[json_field] = json.loads(d[json_field]) if d.get(json_field) else None
        out.append(d)
    return out


def list_supervision_log(*, symbol=None, decision=None, limit=20) -> list:
    clauses, params = [], []
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)
    if decision:
        clauses.append("decision = ?")
        params.append(decision)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM supervision_log {where} ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return _rows_to_dicts(rows, "report_json")


def list_agent_health(*, agent=None, limit=20) -> list:
    clauses, params = [], []
    if agent:
        clauses.append("agent = ?")
        params.append(agent)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM agent_health_snapshots {where} ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return _rows_to_dicts(rows, "snapshot_json")
