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
        # Milestone 9 (agents/runtime/agent_runtime.py) extends this
        # SAME agent_status table -- rather than a second, parallel
        # "runtime_agent_status" table -- with real execution tracking
        # (running/last-execution/duration/failures/health score).
        # Same self-migrating PRAGMA table_info() + ALTER TABLE pattern
        # app.py and agents/memory/sqlite_store.py's own trades_json
        # column already use; safe to run on every process start.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_status)")}
        for col, ddl in (
            ("currently_running", "INTEGER NOT NULL DEFAULT 0"),
            ("last_execution_ts", "TEXT"),
            ("last_execution_duration_ms", "REAL"),
            ("failure_counter", "INTEGER NOT NULL DEFAULT 0"),
            ("health_score", "INTEGER NOT NULL DEFAULT 100"),
            # Milestone 12, Phase 2 Foundation: agents.runtime.scheduling_control's
            # own persisted per-agent scheduling mode ("enabled"/"disabled"/
            # "dry_run") -- extends this SAME table again rather than a new
            # one, matching the exact precedent set by the two migrations
            # above it.
            ("schedule_mode", "TEXT NOT NULL DEFAULT 'enabled'"),
        ):
            if col not in existing_cols:
                conn.execute(f"ALTER TABLE agent_status ADD COLUMN {col} {ddl}")
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


def upsert_agent_status(agent, *, enabled=None, last_heartbeat_ts=None, crashed=None, crash_reason=None,
                         schedule_mode=None) -> None:
    """Only the columns explicitly passed are updated -- e.g. a
    heartbeat call updates last_heartbeat_ts without touching
    enabled/crashed. crash_reason is the one exception to "None means
    don't touch": it's coupled to crashed, not independently settable
    -- passing crashed=False always clears crash_reason to None too
    (a reason without a crash flag is meaningless), even if the caller
    didn't explicitly pass crash_reason. Without this coupling,
    restart_agent()'s crashed=False call would leave a stale
    crash_reason behind forever, since None is otherwise "don't touch."

    `schedule_mode` (Milestone 12, Phase 2 Foundation): "enabled" /
    "disabled" / "dry_run" -- validated by agents.runtime.
    scheduling_control.set_mode() before it ever reaches this function;
    this function itself stays a pure, untyped storage primitive, the
    same contract every other field here already holds to."""
    if crashed is False:
        crash_reason = None
    touch_crash_reason = crash_reason is not None or crashed is False
    conn = _connect()
    try:
        existing = conn.execute("SELECT * FROM agent_status WHERE agent=?", (agent,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO agent_status (agent, enabled, last_heartbeat_ts, crashed, crash_reason, "
                "schedule_mode, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (agent, 1 if enabled is None else int(enabled), last_heartbeat_ts,
                 0 if crashed is None else int(crashed), crash_reason,
                 schedule_mode or "enabled", _now()),
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
            if schedule_mode is not None:
                merged["schedule_mode"] = schedule_mode
            conn.execute(
                "UPDATE agent_status SET enabled=?, last_heartbeat_ts=?, crashed=?, crash_reason=?, "
                "schedule_mode=?, updated_at=? WHERE agent=?",
                (merged["enabled"], merged["last_heartbeat_ts"], merged["crashed"], merged["crash_reason"],
                 merged["schedule_mode"], _now(), agent),
            )
        conn.commit()
    finally:
        conn.close()


def set_currently_running(agent: str, running: bool) -> None:
    """Milestone 9: flips the moment an agent's cycle starts/ends --
    lets a stuck/hung cycle actually be VISIBLE (currently_running=1
    with a last_execution_ts from long ago), which last_heartbeat_ts
    alone can't show since a heartbeat only proves the cycle STARTED."""
    conn = _connect()
    try:
        existing = conn.execute("SELECT 1 FROM agent_status WHERE agent=?", (agent,)).fetchone()
        if existing is None:
            conn.execute(
                "INSERT INTO agent_status (agent, updated_at, currently_running) VALUES (?, ?, ?)",
                (agent, _now(), int(running)),
            )
        else:
            conn.execute(
                "UPDATE agent_status SET currently_running=?, updated_at=? WHERE agent=?",
                (int(running), _now(), agent),
            )
        conn.commit()
    finally:
        conn.close()


def record_execution(agent: str, *, success: bool, duration_ms: float) -> dict:
    """Milestone 9: one call per completed agent cycle. Health score is
    a simple, documented, bounded [0, 100] deduction/recovery scheme --
    -20 per consecutive failure (never below 0), +10 per success (never
    above 100, and only once currently_running has been cleared by the
    same call) -- not a claim of statistical rigor, the same "honest,
    bounded, real" posture as agents.sys_admin.maintenance's leak probe.
    failure_counter tracks CONSECUTIVE failures (reset to 0 on any
    success), read by agent_runtime.py to decide when to escalate rather
    than keep auto-restarting (config.RUNTIME_MAX_CONSECUTIVE_FAILURES_
    BEFORE_ESCALATION). Returns the updated status row."""
    conn = _connect()
    try:
        existing = conn.execute("SELECT * FROM agent_status WHERE agent=?", (agent,)).fetchone()
        prev_failures = existing["failure_counter"] if existing else 0
        prev_health = existing["health_score"] if existing else 100

        if success:
            failure_counter = 0
            health_score = min(100, prev_health + 10)
        else:
            failure_counter = prev_failures + 1
            health_score = max(0, prev_health - 20)

        now = _now()
        if existing is None:
            conn.execute(
                "INSERT INTO agent_status (agent, updated_at, currently_running, last_execution_ts, "
                "last_execution_duration_ms, failure_counter, health_score) VALUES (?, ?, 0, ?, ?, ?, ?)",
                (agent, now, now, duration_ms, failure_counter, health_score),
            )
        else:
            conn.execute(
                "UPDATE agent_status SET currently_running=0, last_execution_ts=?, "
                "last_execution_duration_ms=?, failure_counter=?, health_score=?, updated_at=? WHERE agent=?",
                (now, duration_ms, failure_counter, health_score, now, agent),
            )
        conn.commit()
        return dict(conn.execute("SELECT * FROM agent_status WHERE agent=?", (agent,)).fetchone())
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
