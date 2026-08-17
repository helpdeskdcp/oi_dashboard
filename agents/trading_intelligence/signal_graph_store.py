"""
agents/trading_intelligence/signal_graph_store.py -- SQLite persistence
for the LangGraph shadow-signal layer (post-launch upgrade, Phase 2:
"a minimal LangGraph shadow graph wrapping only the already-existing
nodes... writing its output to a new read-only table, comparable
against today's real engine before any pattern-memory or Obsidian work
starts").

Same module-level DB_PATH + per-call connect/close + PRAGMA busy_timeout
convention every prior agent store module (ti_store.py, virtual_trailing.py)
already established. A NEW table (`ti_signal_graph_shadow`), never a reuse
of ti_signal_log -- that table is the REAL engine's own explainability
log; this one is the shadow graph's OWN observation, kept structurally
separate so nothing about the real engine's existing behavior/schema
changes for this experiment.

Read-only from the rest of the application's perspective: nothing here
is ever read back to influence paper_trading.enter_from_recommendation()
or telegram_notifier -- this table exists purely so a human (or a later
comparison script) can inspect shadow-graph output against the real
engine's own decision for the same cycle.
"""
import json
import sqlite3

from .. import timekeeping

DB_PATH = "oi_history.db"


def _now() -> str:
    return timekeeping.now_ist_iso()


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
            CREATE TABLE IF NOT EXISTS ti_signal_graph_shadow (
                id                           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts                           TEXT NOT NULL,
                symbol                       TEXT NOT NULL,
                data_available               INTEGER NOT NULL,
                regime_trend                 TEXT,
                regime_volatility             TEXT,
                institutional_finding_count   INTEGER,
                graph_action                 TEXT,
                graph_direction              TEXT,
                graph_confidence             INTEGER,
                timeframe_alignment_score     REAL,
                timeframe_alignment_label     TEXT,
                real_engine_action           TEXT,
                agrees_with_real_engine       INTEGER,
                total_latency_ms             REAL,
                node_latencies_json          TEXT,
                node_errors_json             TEXT,
                error                        TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_ti_signal_graph_shadow_symbol_ts
                ON ti_signal_graph_shadow(symbol, ts);
            """
        )
        conn.commit()
    finally:
        conn.close()


def record(result: dict) -> int:
    """Writes one shadow-graph run's result. `result` is the dict shape
    signal_graph.run_shadow() returns -- see that module's own docstring
    for the exact keys. Returns the new row's id."""
    conn = _connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO ti_signal_graph_shadow (
                ts, symbol, data_available, regime_trend, regime_volatility,
                institutional_finding_count, graph_action, graph_direction,
                graph_confidence, timeframe_alignment_score, timeframe_alignment_label,
                real_engine_action, agrees_with_real_engine, total_latency_ms,
                node_latencies_json, node_errors_json, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now(), result["symbol"], int(bool(result.get("data_available"))),
                result.get("regime_trend"), result.get("regime_volatility"),
                result.get("institutional_finding_count"), result.get("graph_action"),
                result.get("graph_direction"), result.get("graph_confidence"),
                result.get("timeframe_alignment_score"), result.get("timeframe_alignment_label"),
                result.get("real_engine_action"),
                None if result.get("agrees_with_real_engine") is None else int(result["agrees_with_real_engine"]),
                result.get("total_latency_ms"),
                json.dumps(result.get("node_latencies") or {}),
                json.dumps(result.get("node_errors")) if result.get("node_errors") else None,
                result.get("error"),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def recent(limit: int = 100) -> list[dict]:
    """Most recent shadow-graph rows, newest first -- for a future
    comparison script/dashboard panel, never used to drive a live
    decision."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM ti_signal_graph_shadow ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
