"""
agents/runtime/runtime_store.py -- SQLite persistence for Milestone 9's
own new state: the task queue, workflow instances + their restart
history, and a live policy override. Same module-level DB_PATH +
per-call connect/close + PRAGMA busy_timeout convention every prior
agent store module already established (agents/audit_log.py,
agents/event_bus.py, agents/risk_manager/risk_store.py,
agents/trading_supervisor/supervision_store.py,
agents/sys_admin/sysadmin_store.py), indexed from the start.

Agent execution status itself is NOT a new table here -- it extends
agents.sys_admin.sysadmin_store's existing agent_status table (see that
module's Milestone 9 section) rather than duplicating it, per "prefer
extension over replacement."
"""
import datetime as dt
import json
import sqlite3

DB_PATH = "oi_history.db"

PRIORITIES = ("high", "medium", "low")
TASK_STATUSES = ("queued", "running", "completed", "failed", "retrying", "dead")
WORKFLOW_STATUSES = ("running", "waiting_approval", "completed", "failed", "cancelled")


def _now() -> str:
    return dt.datetime.now().isoformat()


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
            CREATE TABLE IF NOT EXISTS runtime_task_queue (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts     TEXT NOT NULL,
                updated_ts     TEXT NOT NULL,
                priority       TEXT NOT NULL,
                task_type      TEXT NOT NULL,
                payload_json   TEXT,
                status         TEXT NOT NULL,
                attempts       INTEGER NOT NULL DEFAULT 0,
                max_attempts   INTEGER NOT NULL,
                next_attempt_ts TEXT NOT NULL,
                last_error     TEXT
            );

            CREATE TABLE IF NOT EXISTS runtime_workflow (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                created_ts     TEXT NOT NULL,
                updated_ts     TEXT NOT NULL,
                workflow_type  TEXT NOT NULL,
                symbol         TEXT,
                current_stage  TEXT NOT NULL,
                status         TEXT NOT NULL,
                state_json     TEXT NOT NULL,
                audit_log_id   INTEGER
            );

            CREATE TABLE IF NOT EXISTS runtime_workflow_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id    INTEGER NOT NULL,
                ts             TEXT NOT NULL,
                stage          TEXT NOT NULL,
                status         TEXT NOT NULL,
                detail_json    TEXT
            );

            CREATE TABLE IF NOT EXISTS runtime_policy (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                policy     TEXT NOT NULL,
                changed_by TEXT,
                reason     TEXT,
                updated_ts TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_runtime_queue_status_priority
                ON runtime_task_queue(status, priority, next_attempt_ts);
            CREATE INDEX IF NOT EXISTS idx_runtime_workflow_status ON runtime_workflow(status);
            CREATE INDEX IF NOT EXISTS idx_runtime_workflow_history_workflow
                ON runtime_workflow_history(workflow_id, ts);
            """
        )
        conn.commit()
    finally:
        conn.close()


# --- Task queue ---------------------------------------------------------

def enqueue(*, priority: str, task_type: str, payload: dict | None = None, max_attempts: int) -> int:
    if priority not in PRIORITIES:
        raise ValueError(f"priority must be one of {PRIORITIES}, got {priority!r}")
    conn = _connect()
    try:
        now = _now()
        cur = conn.execute(
            "INSERT INTO runtime_task_queue (created_ts, updated_ts, priority, task_type, payload_json, "
            "status, attempts, max_attempts, next_attempt_ts) VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?)",
            (now, now, priority, task_type, json.dumps(payload) if payload is not None else None,
             max_attempts, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _row_to_dict(row) -> dict:
    d = dict(row)
    if d.get("payload_json"):
        d["payload_json"] = json.loads(d["payload_json"])
    return d


def claim_next_task(*, as_of_ts: str | None = None) -> dict | None:
    """Atomically claims the highest-priority, oldest, due task -- one
    SQLite transaction (SELECT the candidate id, then an UPDATE guarded
    by status='queued' on that exact id) so two concurrent callers can
    never both claim the same row; if the guarded UPDATE affects zero
    rows, another caller won the race and this one simply returns None
    rather than retrying (the caller's own next poll picks up whatever's
    left)."""
    as_of_ts = as_of_ts or _now()
    priority_order = "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END"
    conn = _connect()
    try:
        candidate = conn.execute(
            f"SELECT id FROM runtime_task_queue WHERE status IN ('queued', 'retrying') "
            f"AND next_attempt_ts <= ? ORDER BY {priority_order}, next_attempt_ts LIMIT 1",
            (as_of_ts,),
        ).fetchone()
        if candidate is None:
            return None
        task_id = candidate["id"]
        cur = conn.execute(
            "UPDATE runtime_task_queue SET status='running', updated_ts=?, attempts=attempts+1 "
            "WHERE id=? AND status IN ('queued', 'retrying')",
            (_now(), task_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute("SELECT * FROM runtime_task_queue WHERE id=?", (task_id,)).fetchone()
        return _row_to_dict(row)
    finally:
        conn.close()


def complete_task(task_id: int) -> None:
    conn = _connect()
    try:
        conn.execute(
            "UPDATE runtime_task_queue SET status='completed', updated_ts=? WHERE id=?", (_now(), task_id)
        )
        conn.commit()
    finally:
        conn.close()


def fail_task(task_id: int, *, error: str, retry_backoff_seconds: float) -> str:
    """Moves a running task to 'retrying' (with a backoff-delayed
    next_attempt_ts) if it hasn't exhausted max_attempts yet, else
    'dead' (the failed queue -- terminal, never auto-retried again).
    Returns the resulting status."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM runtime_task_queue WHERE id=?", (task_id,)).fetchone()
        if row is None:
            raise ValueError(f"no task with id={task_id}")
        if row["attempts"] >= row["max_attempts"]:
            status = "dead"
            next_attempt = row["next_attempt_ts"]
        else:
            status = "retrying"
            next_attempt = (dt.datetime.now() + dt.timedelta(seconds=retry_backoff_seconds)).isoformat()
        conn.execute(
            "UPDATE runtime_task_queue SET status=?, updated_ts=?, next_attempt_ts=?, last_error=? WHERE id=?",
            (status, _now(), next_attempt, error, task_id),
        )
        conn.commit()
        return status
    finally:
        conn.close()


def list_tasks(*, status: str | None = None, priority: str | None = None, limit: int = 50) -> list:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if priority:
        clauses.append("priority = ?")
        params.append(priority)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM runtime_task_queue {where} ORDER BY created_ts DESC LIMIT ?"
    params.append(limit)
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def queue_depth() -> dict:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT status, COUNT(*) as n FROM runtime_task_queue "
            "WHERE status IN ('queued', 'running', 'retrying', 'dead') GROUP BY status"
        ).fetchall()
    finally:
        conn.close()
    depth = {"queued": 0, "running": 0, "retrying": 0, "dead": 0}
    depth.update({r["status"]: r["n"] for r in rows})
    return depth


# --- Workflow state -------------------------------------------------------

def create_workflow(*, workflow_type: str, symbol: str | None, first_stage: str, state: dict) -> int:
    conn = _connect()
    try:
        now = _now()
        cur = conn.execute(
            "INSERT INTO runtime_workflow (created_ts, updated_ts, workflow_type, symbol, current_stage, "
            "status, state_json) VALUES (?, ?, ?, ?, ?, 'running', ?)",
            (now, now, workflow_type, symbol, first_stage, json.dumps(state)),
        )
        conn.commit()
        workflow_id = cur.lastrowid
    finally:
        conn.close()
    _append_history(workflow_id, stage=first_stage, status="started", detail={})
    return workflow_id


def update_workflow(workflow_id: int, *, current_stage: str | None = None, status: str | None = None,
                     state: dict | None = None, audit_log_id: int | None = None) -> None:
    conn = _connect()
    try:
        existing = conn.execute("SELECT * FROM runtime_workflow WHERE id=?", (workflow_id,)).fetchone()
        if existing is None:
            raise ValueError(f"no workflow with id={workflow_id}")
        conn.execute(
            "UPDATE runtime_workflow SET current_stage=?, status=?, state_json=?, "
            "audit_log_id=COALESCE(?, audit_log_id), updated_ts=? WHERE id=?",
            (
                current_stage or existing["current_stage"], status or existing["status"],
                json.dumps(state) if state is not None else existing["state_json"],
                audit_log_id, _now(), workflow_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _append_history(workflow_id: int, *, stage: str, status: str, detail: dict) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO runtime_workflow_history (workflow_id, ts, stage, status, detail_json) "
            "VALUES (?, ?, ?, ?, ?)",
            (workflow_id, _now(), stage, status, json.dumps(detail)),
        )
        conn.commit()
    finally:
        conn.close()


def record_stage_transition(workflow_id: int, *, stage: str, status: str, detail: dict | None = None) -> None:
    _append_history(workflow_id, stage=stage, status=status, detail=detail or {})


def get_workflow(workflow_id: int) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM runtime_workflow WHERE id=?", (workflow_id,)).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    d = dict(row)
    d["state_json"] = json.loads(d["state_json"])
    return d


def list_workflows(*, status: str | None = None, limit: int = 50) -> list:
    clauses, params = [], []
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT * FROM runtime_workflow {where} ORDER BY updated_ts DESC LIMIT ?"
    params.append(limit)
    conn = _connect()
    try:
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["state_json"] = json.loads(d["state_json"])
        out.append(d)
    return out


def workflow_history(workflow_id: int) -> list:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM runtime_workflow_history WHERE workflow_id=? ORDER BY ts", (workflow_id,)
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["detail_json"] = json.loads(d["detail_json"]) if d.get("detail_json") else {}
        out.append(d)
    return out


def resumable_workflows() -> list:
    """Workflows left 'running' or 'waiting_approval' -- what a restarted
    scheduler resumes instead of losing (see agents/runtime/scheduler.py's
    "never lose workflow state" requirement)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM runtime_workflow WHERE status IN ('running', 'waiting_approval') ORDER BY created_ts"
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["state_json"] = json.loads(d["state_json"])
        out.append(d)
    return out


# --- Policy override --------------------------------------------------------

def get_policy_override() -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM runtime_policy WHERE id=1").fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def set_policy_override(policy: str, *, changed_by: str, reason: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO runtime_policy (id, policy, changed_by, reason, updated_ts) VALUES (1, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET policy=excluded.policy, changed_by=excluded.changed_by, "
            "reason=excluded.reason, updated_ts=excluded.updated_ts",
            (policy, changed_by, reason, _now()),
        )
        conn.commit()
    finally:
        conn.close()
