"""
agents/sys_admin/sysadmin_store.py -- SQLite persistence for the System
Administrator: every autonomous action's report (sysadmin_log), current
per-agent orchestration state (agent_status), and backup metadata
(backups). Same module-level DB_PATH + per-call connect/close +
PRAGMA busy_timeout convention every prior agent's store module already
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
            CREATE TABLE IF NOT EXISTS sysadmin_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                module        TEXT NOT NULL,
                action        TEXT NOT NULL,
                severity      TEXT NOT NULL,
                confidence    INTEGER NOT NULL,
                report_json   TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS agent_status (
                agent             TEXT PRIMARY KEY,
                enabled           INTEGER NOT NULL DEFAULT 1,
                last_heartbeat_ts TEXT,
                crashed           INTEGER NOT NULL DEFAULT 0,
                crash_reason      TEXT,
                updated_at        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS backups (
                id                     INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                     TEXT NOT NULL,
                backup_path            TEXT NOT NULL,
                source_db_path         TEXT NOT NULL,
                size_bytes             INTEGER,
                verified               INTEGER NOT NULL DEFAULT 0,
                integrity_ok           INTEGER,
                restored_from_backup_id INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_sysadmin_log_module_ts ON sysadmin_log(module, ts);
            CREATE INDEX IF NOT EXISTS idx_sysadmin_log_severity_ts ON sysadmin_log(severity, ts);
            CREATE INDEX IF NOT EXISTS idx_backups_ts ON backups(ts);
            CREATE INDEX IF NOT EXISTS idx_backups_verified ON backups(verified, integrity_ok);
            """
        )
        conn.commit()
    finally:
        conn.close()


def record_report(report) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO sysadmin_log (ts, module, action, severity, confidence, report_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_now(), report.module, report.action, report.severity, report.confidence, report.to_json()),
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


def list_reports(*, module=None, severity=None, limit=20) -> list:
    clauses, params = [], []
    if module:
        clauses.append("module = ?")
        params.append(module)
    if severity:
        clauses.append("severity = ?")
        params.append(severity)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM sysadmin_log {where} ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return _rows_to_dicts(rows, "report_json")


def upsert_agent_status(agent, *, enabled=None, last_heartbeat_ts=None, crashed=None, crash_reason=None) -> None:
    """Only the columns explicitly passed are updated -- e.g. a
    heartbeat call updates last_heartbeat_ts without touching
    enabled/crashed. crash_reason is the one exception to "None means
    don't touch": it's coupled to crashed, not independently settable
    -- passing crashed=False always clears crash_reason to None too
    (a reason without a crash flag is meaningless), even if the caller
    didn't explicitly pass crash_reason. Without this coupling,
    restart_agent()'s crashed=False call would leave a stale
    crash_reason behind forever, since None is otherwise "don't touch.\""""
    if crashed is False:
        crash_reason = None
    touch_crash_reason = crash_reason is not None or crashed is False
    conn = _connect()
    try:
        existing = conn.execute("SELECT * FROM agent_status WHERE agent=?", (agent,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO agent_status (agent, enabled, last_heartbeat_ts, crashed, crash_reason, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (agent, 1 if enabled is None else int(enabled), last_heartbeat_ts,
                 0 if crashed is None else int(crashed), crash_reason, _now()),
            )
        else:
            merged = dict(existing)
            if enabled is not None:
                merged["enabled"] = int(enabled)
            if last_heartbeat_ts is not None:
                merged["last_heartbeat_ts"] = last_heartbeat_ts
            if crashed is not None:
                merged["crashed"] = int(crashed)
            if touch_crash_reason:
                merged["crash_reason"] = crash_reason
            conn.execute(
                "UPDATE agent_status SET enabled=?, last_heartbeat_ts=?, crashed=?, crash_reason=?, updated_at=? "
                "WHERE agent=?",
                (merged["enabled"], merged["last_heartbeat_ts"], merged["crashed"], merged["crash_reason"],
                 _now(), agent),
            )
        conn.commit()
    finally:
        conn.close()


def get_agent_status(agent) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM agent_status WHERE agent=?", (agent,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_agent_status() -> list:
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM agent_status ORDER BY agent").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def record_backup(*, backup_path, source_db_path, size_bytes, verified=False, integrity_ok=None,
                   restored_from_backup_id=None) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO backups (ts, backup_path, source_db_path, size_bytes, verified, integrity_ok, "
            "restored_from_backup_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_now(), backup_path, source_db_path, size_bytes, 1 if verified else 0,
             (1 if integrity_ok else 0) if integrity_ok is not None else None, restored_from_backup_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_backups(*, verified_only=False, limit=20) -> list:
    clauses, params = [], []
    if verified_only:
        clauses.append("verified = 1 AND integrity_ok = 1")
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM backups {where} ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
