"""
agents/risk_manager/risk_store.py -- SQLite persistence for every risk
decision this agent makes (risk_assessments, risk_alerts, risk_snapshots
tables in oi_history.db). Same module-level DB_PATH + per-call
connect/close convention agents/audit_log.py and agents/event_bus.py
already established -- tests monkeypatch risk_store.DB_PATH exactly like
they already monkeypatch audit_log.DB_PATH/event_bus.DB_PATH (see
test_agents/conftest.py's agent_db fixture). Indexes are defined from
the start here, not retrofitted after the fact -- an explicit lesson
from the architecture review that ran just before this milestone began.
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
            CREATE TABLE IF NOT EXISTS risk_assessments (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ts               TEXT NOT NULL,
                candidate_name   TEXT,
                symbol           TEXT,
                strategy_family  TEXT,
                risk_score       INTEGER,
                decision         TEXT NOT NULL,
                report_json      TEXT NOT NULL,
                audit_log_id     INTEGER
            );

            CREATE TABLE IF NOT EXISTS risk_alerts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              TEXT NOT NULL,
                metric          TEXT NOT NULL,
                severity        TEXT NOT NULL,
                message         TEXT NOT NULL,
                value           REAL,
                limit_value     REAL,
                user_id         INTEGER,
                recommendation  TEXT,
                report_json     TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS risk_snapshots (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                  TEXT NOT NULL,
                user_id             INTEGER,
                exposure            REAL,
                portfolio_heat      REAL,
                margin_utilization  REAL,
                daily_pnl           REAL,
                max_drawdown        REAL,
                snapshot_json       TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_risk_assessments_symbol_ts ON risk_assessments(symbol, ts);
            CREATE INDEX IF NOT EXISTS idx_risk_assessments_decision ON risk_assessments(decision);
            CREATE INDEX IF NOT EXISTS idx_risk_alerts_metric_ts ON risk_alerts(metric, ts);
            CREATE INDEX IF NOT EXISTS idx_risk_alerts_severity_ts ON risk_alerts(severity, ts);
            CREATE INDEX IF NOT EXISTS idx_risk_snapshots_user_ts ON risk_snapshots(user_id, ts);
            """
        )
        conn.commit()
    finally:
        conn.close()


def record_assessment(report, *, candidate_name, symbol, strategy_family, audit_log_id=None) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO risk_assessments "
            "(ts, candidate_name, symbol, strategy_family, risk_score, decision, report_json, audit_log_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _now(), candidate_name, symbol, strategy_family, report.risk_score, report.decision,
                report.to_json(), audit_log_id,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def record_alert(report, *, metric, severity, value=None, limit_value=None, user_id=None,
                  recommendation=None) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO risk_alerts "
            "(ts, metric, severity, message, value, limit_value, user_id, recommendation, report_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), metric, severity, report.summary, value, limit_value, user_id, recommendation, report.to_json()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def record_snapshot(report, *, user_id=None, exposure=None, portfolio_heat=None,
                     margin_utilization=None, daily_pnl=None, max_drawdown=None) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO risk_snapshots "
            "(ts, user_id, exposure, portfolio_heat, margin_utilization, daily_pnl, max_drawdown, snapshot_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), user_id, exposure, portfolio_heat, margin_utilization, daily_pnl, max_drawdown,
             report.to_json()),
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


def list_assessments(*, symbol=None, decision=None, limit=20) -> list:
    clauses, params = [], []
    if symbol:
        clauses.append("symbol = ?")
        params.append(symbol)
    if decision:
        clauses.append("decision = ?")
        params.append(decision)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM risk_assessments {where} ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return _rows_to_dicts(rows, "report_json")


def list_alerts(*, severity=None, user_id=None, limit=20) -> list:
    clauses, params = [], []
    if severity:
        clauses.append("severity = ?")
        params.append(severity)
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM risk_alerts {where} ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return _rows_to_dicts(rows, "report_json")


def list_snapshots(*, user_id=None, limit=20) -> list:
    clauses, params = [], []
    if user_id is not None:
        clauses.append("user_id = ?")
        params.append(user_id)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM risk_snapshots {where} ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return _rows_to_dicts(rows, "snapshot_json")
