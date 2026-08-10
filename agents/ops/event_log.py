"""
agents/ops/event_log.py -- Milestone 16, Phase 1: Persistent Runtime
Event Log. Own dedicated table (ops_event_log), CREATE TABLE IF NOT
EXISTS only -- deliberately NOT agents.event_bus's own agent_events
table, even though a mature, typed event system already exists there
(agents/runtime/runtime_events.py, backing agents/event_bus.py).
agent_events holds audit-significant governance events (POLICY_CHANGED,
APPROVAL_GRANTED/REJECTED, AGENT_ESCALATED, WORKFLOW_*) that must never
be subject to a casual time-based retention purge -- this module's own
purge_old_events() is an explicit requirement, and running it against
agent_events would be a real audit-trail/compliance risk in a financial
system. This table is scoped to high-volume, genuinely disposable
OPERATIONAL telemetry (heartbeats, alert deliveries, rate-limit hits,
retry attempts, circuit-breaker transitions, watchdog checks) --
exactly the kind of thing a 30-day retention window is safe to apply
to. Matches this codebase's own established convention of one
dedicated table per concern (see agents/intelligence_alerts/*.py's own
several separate tables) rather than one shared table for everything.
"""
import datetime as dt
import json
import logging
import sqlite3

from . import models

DB_PATH = "oi_history.db"
logger = logging.getLogger("oi_dashboard.ops.event_log")


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
            CREATE TABLE IF NOT EXISTS ops_event_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT NOT NULL,
                event_type   TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ops_event_log_ts ON ops_event_log(ts);
            CREATE INDEX IF NOT EXISTS idx_ops_event_log_event_type_ts ON ops_event_log(event_type, ts);
            """
        )
        conn.commit()
    finally:
        conn.close()


def record_event(event_type: str, payload: dict, *, now=None) -> int:
    """Validates event_type against models.ALL_EVENT_TYPES -- a typo
    here should fail loudly, not silently create an unqueryable event
    type nothing will ever filter on (same discipline as
    agents.runtime.runtime_events.emit())."""
    if event_type not in models.ALL_EVENT_TYPES:
        raise ValueError(f"unknown ops event_type {event_type!r} -- add it to agents/ops/models.py first")
    now = now or dt.datetime.now()
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO ops_event_log (ts, event_type, payload_json) VALUES (?,?,?)",
            (now.isoformat(), event_type, json.dumps(payload)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def record_event_safe(event_type: str, payload: dict, *, now=None) -> None:
    """Same guarantee as agents.runtime.runtime_events.emit_safe(): a
    failure to write this operational telemetry event (most commonly,
    a caller that hasn't run init_db() yet, e.g. an existing test not
    exercising this new integration) must never propagate into the
    caller's own control flow -- dedup_store.py/rate_limiter.py/
    retry_tracker.py/circuit_breaker.py all call this, not
    record_event() directly, from their existing log points."""
    try:
        record_event(event_type, payload, now=now)
    except Exception:
        logger.exception("failed to record a %r ops event -- continuing without it", event_type)


def get_events(*, limit: int = 100, offset: int = 0, event_type: str | None = None) -> list:
    """Newest-first, paginated. Each row's payload is decoded back to a
    dict (stored as JSON text) -- callers never see the raw JSON
    string."""
    conn = _connect()
    try:
        sql = "SELECT * FROM ops_event_log "
        params: list = []
        if event_type:
            sql += "WHERE event_type = ? "
            params.append(event_type)
        sql += "ORDER BY ts DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [
        {"id": r["id"], "ts": r["ts"], "event_type": r["event_type"], "payload": json.loads(r["payload_json"])}
        for r in rows
    ]


def count_events(event_type: str | None = None) -> int:
    conn = _connect()
    try:
        if event_type:
            return conn.execute(
                "SELECT COUNT(*) FROM ops_event_log WHERE event_type = ?", (event_type,)
            ).fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM ops_event_log").fetchone()[0]
    finally:
        conn.close()


def purge_old_events(retention_days: int, *, now=None) -> int:
    """Deletes rows older than retention_days -- returns the number of
    rows deleted. Only ever touches THIS module's own ops_event_log
    table -- never agents.event_bus's agent_events (see this module's
    own docstring for why that distinction matters)."""
    now = now or dt.datetime.now()
    cutoff = (now - dt.timedelta(days=retention_days)).isoformat()
    conn = _connect()
    try:
        cur = conn.execute("DELETE FROM ops_event_log WHERE ts < ?", (cutoff,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
