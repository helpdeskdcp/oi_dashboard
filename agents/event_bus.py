"""
agents/event_bus.py -- SQLite-backed publish/poll event log
(agent_events table), NOT a message broker. See
AUTONOMOUS_AGENTS_ARCHITECTURE.md principle 3: this is a single-VPS
deployment already built entirely on SQLite -- a distributed broker would
solve a problem BATI doesn't have.

Milestone 1 has exactly one future publisher (agents/dev_agent/, Milestone
2+) and no orchestrator/dispatcher yet, so on_event() subscription is
unused for now. This module exists at Milestone 1 anyway because
AI_DEVELOPER_AGENT_PLAN.md's execution triggers (new commit, failed
tests, failed backtest, runtime exception, performance regression -- see
"Execution Cadence") are themselves most naturally modeled as rows landing
in this table, not as a separate trigger mechanism.
"""
import datetime as dt
import json
import sqlite3

DB_PATH = "oi_history.db"

VALID_SEVERITIES = ("info", "warning", "critical")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Idempotent -- CREATE TABLE IF NOT EXISTS, safe on every process start."""
    conn = _connect()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_events (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                source_agent  TEXT NOT NULL,
                event_type    TEXT NOT NULL,
                payload_json  TEXT NOT NULL,
                severity      TEXT NOT NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def publish(source_agent, event_type, payload, severity="info"):
    """Appends one event. payload must be JSON-serializable -- this is a
    durable log, not a Python object passthrough, so anything a future
    subscriber needs must already be plain data by the time it's
    published."""
    if severity not in VALID_SEVERITIES:
        raise ValueError(f"severity must be one of {VALID_SEVERITIES}, got {severity!r}")
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO agent_events (ts, source_agent, event_type, payload_json, severity) "
            "VALUES (?, ?, ?, ?, ?)",
            (dt.datetime.now().isoformat(), source_agent, event_type, json.dumps(payload), severity),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def events_since(since_ts, event_type=None):
    """Events with ts > since_ts, oldest first, payload_json parsed back
    into a dict. event_type=None returns every event type."""
    conn = _connect()
    try:
        if event_type:
            rows = conn.execute(
                "SELECT * FROM agent_events WHERE ts > ? AND event_type=? ORDER BY ts",
                (since_ts, event_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agent_events WHERE ts > ? ORDER BY ts", (since_ts,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload_json"] = json.loads(d["payload_json"])
            out.append(d)
        return out
    finally:
        conn.close()
