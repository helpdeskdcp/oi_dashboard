"""
agents/audit_log.py -- append-only audit trail for all BATI autonomous
agents (agent_audit_log table). See AUTONOMOUS_AGENTS_ARCHITECTURE.md's
"Communication protocol" section for why this is two plain SQLite tables
rather than a message broker.

No function in this module ever deletes a row or overwrites the fields
that describe WHAT happened -- set_outcome() only ever transitions
outcome/approved_by/approved_at/*_sha on an existing row, never touching
agent/action_type/description/payload_json/risk_tier. There is
deliberately no delete_* function in this module at all.
"""
import datetime as dt
import json
import sqlite3

DB_PATH = "oi_history.db"

VALID_OUTCOMES = ("pending_approval", "approved", "rejected", "applied", "failed", "rolled_back")
VALID_ACTION_TYPES = ("finding", "gate_result", "proposal", "approval", "rejection", "applied", "rollback")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Matches app.py/backtest.py's own _connect() -- oi_history.db is
    # shared by the live app and every agent; without this, a write that
    # collides with another writer's open transaction fails immediately
    # ("database is locked") instead of waiting briefly, as more agents
    # (Milestone 6+) start writing to this same file concurrently.
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    """Idempotent (CREATE TABLE IF NOT EXISTS) -- safe to call on every
    process start, matching app.py's own init_db() convention."""
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_audit_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                ts               TEXT NOT NULL,
                agent            TEXT NOT NULL,
                action_type      TEXT NOT NULL,
                description      TEXT NOT NULL,
                payload_json     TEXT,
                risk_tier        TEXT NOT NULL,
                outcome          TEXT NOT NULL,
                approved_by      TEXT,
                approved_at      TEXT,
                pre_merge_sha    TEXT,
                merge_commit_sha TEXT
            )
            """
        )
        # list_pending() filters on outcome; most other reads/audits filter
        # by agent within a time range -- unindexed full-table scans today,
        # cheap while the table is small, increasingly not as more agents
        # (Milestone 6+) write to it daily.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_audit_log_outcome ON agent_audit_log(outcome)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_agent_audit_log_agent_ts ON agent_audit_log(agent, ts)")
        conn.commit()
    finally:
        conn.close()


def record(agent, action_type, description, risk_tier, outcome, payload=None):
    """Appends one row. Returns the new row's id. Raises ValueError for an
    outcome/action_type outside the known set -- a typo here should fail
    loudly at the call site, not silently create an unqueryable row."""
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"outcome must be one of {VALID_OUTCOMES}, got {outcome!r}")
    if action_type not in VALID_ACTION_TYPES:
        raise ValueError(f"action_type must be one of {VALID_ACTION_TYPES}, got {action_type!r}")
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO agent_audit_log (ts, agent, action_type, description, payload_json, risk_tier, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                dt.datetime.now().isoformat(), agent, action_type, description,
                json.dumps(payload) if payload is not None else None, risk_tier, outcome,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get(row_id):
    """Returns the row as a dict (payload_json parsed back into a dict
    under the same key), or None if row_id doesn't exist."""
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM agent_audit_log WHERE id=?", (row_id,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["payload_json"] = json.loads(d["payload_json"]) if d["payload_json"] else None
        return d
    finally:
        conn.close()


def list_pending(agent=None):
    """Rows with outcome='pending_approval', oldest first -- what
    approve_cli.py (Milestone 4) lists for a human to act on."""
    conn = _connect()
    try:
        if agent:
            rows = conn.execute(
                "SELECT * FROM agent_audit_log WHERE outcome='pending_approval' AND agent=? ORDER BY ts",
                (agent,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_audit_log WHERE outcome='pending_approval' ORDER BY ts"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def set_outcome(row_id, outcome, *, approved_by=None, pre_merge_sha=None, merge_commit_sha=None):
    """Transitions row_id's outcome (e.g. pending_approval -> approved,
    approved -> applied, applied -> rolled_back). Only the outcome/
    approval/SHA columns are ever touched -- the original finding/
    proposal fields (agent, description, payload_json, risk_tier) are
    immutable once written. Raises ValueError if row_id doesn't exist, so
    a caller passing a stale/typo'd id fails loudly instead of silently
    updating zero rows."""
    if outcome not in VALID_OUTCOMES:
        raise ValueError(f"outcome must be one of {VALID_OUTCOMES}, got {outcome!r}")
    conn = _connect()
    try:
        cur = conn.execute(
            "UPDATE agent_audit_log SET outcome=?, approved_by=?, approved_at=?, "
            "pre_merge_sha=COALESCE(?, pre_merge_sha), merge_commit_sha=COALESCE(?, merge_commit_sha) "
            "WHERE id=?",
            (
                outcome, approved_by, dt.datetime.now().isoformat() if approved_by else None,
                pre_merge_sha, merge_commit_sha, row_id,
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            raise ValueError(f"no agent_audit_log row with id={row_id}")
    finally:
        conn.close()
