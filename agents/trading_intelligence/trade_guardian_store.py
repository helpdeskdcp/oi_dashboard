"""
agents/trading_intelligence/trade_guardian_store.py -- SQLite persistence
for the Smart Mythos Trade Guardian (post-launch upgrade, shadow/advisory
phase). Same module-level DB_PATH + per-call connect/close + PRAGMA
busy_timeout convention every prior store module in this package already
established (ti_store.py, virtual_trailing.py, signal_graph_store.py).

Three tables, position_id (agents.trading_intelligence.trade_guardian.
_position_id() -- the one place it's built) the shared key across all three:

  trade_guardian_plan          -- the Administrator's ORIGINAL trade plan,
                                   captured once at registration and
                                   IMMUTABLE afterward: register_plan()
                                   below has no UPDATE path for an existing
                                   row's own fields, only INSERT-if-absent.
                                   Never inferred from a Telegram signal or
                                   overwritten by a later Guardian
                                   recommendation (Section 1's explicit
                                   requirement).
  trade_guardian_state         -- the CURRENT Guardian state machine +
                                   latest Smart Target/Smart SL/health
                                   score for one position. Upserted every
                                   evaluation cycle, restart-safe (DB-
                                   persisted, same pattern virtual_trailing_
                                   state already established).
  trade_guardian_decision_log  -- append-only history of every evaluation's
                                   full reasoning. Never updated or deleted
                                   -- the audit trail kept "structured and
                                   future-memory friendly" so a later
                                   Obsidian adapter (explicitly deferred
                                   from this PR) can read completed trade
                                   outcomes without the Guardian itself
                                   changing.
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
            CREATE TABLE IF NOT EXISTS trade_guardian_plan (
                position_id      TEXT PRIMARY KEY,
                symbol           TEXT NOT NULL,
                expiry           TEXT,
                strike           REAL NOT NULL,
                direction        TEXT NOT NULL,
                entry_price      REAL NOT NULL,
                quantity         INTEGER NOT NULL,
                original_sl      REAL NOT NULL,
                original_t1      REAL NOT NULL,
                original_t2      REAL,
                original_t3      REAL,
                entry_timestamp  TEXT NOT NULL,
                signal_reference TEXT,
                registered_at    TEXT NOT NULL,
                registered_by    TEXT
            );

            CREATE TABLE IF NOT EXISTS trade_guardian_state (
                position_id           TEXT PRIMARY KEY,
                state                 TEXT NOT NULL,
                smart_sl              REAL,
                smart_target_low      REAL,
                smart_target_high     REAL,
                breakout_target       REAL,
                trade_health_score    REAL,
                trade_health_tier     TEXT,
                action                TEXT,
                reason                TEXT,
                last_updated          TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS trade_guardian_decision_log (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                position_id              TEXT NOT NULL,
                ts                       TEXT NOT NULL,
                state                    TEXT,
                underlying_ltp           REAL,
                current_premium          REAL,
                smart_sl                 REAL,
                smart_target_low         REAL,
                smart_target_high        REAL,
                breakout_target          REAL,
                trade_health_score       REAL,
                trade_health_tier        TEXT,
                action                   TEXT,
                reason                   TEXT,
                component_scores_json    TEXT,
                target_feasibility_json  TEXT,
                data_quality_json        TEXT,
                error                    TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_trade_guardian_decision_log_position_ts
                ON trade_guardian_decision_log(position_id, ts);
            """
        )
        conn.commit()
    finally:
        conn.close()


def register_plan(plan: dict) -> str:
    """Registers a NEW original trade plan. IMMUTABLE: if position_id
    already exists, returns the existing row's own id untouched -- never
    overwrites, never re-inserts (Section 1's explicit requirement)."""
    position_id = plan["position_id"]
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT position_id FROM trade_guardian_plan WHERE position_id=?", (position_id,)
        ).fetchone()
        if existing:
            return position_id
        conn.execute(
            """INSERT INTO trade_guardian_plan (
                position_id, symbol, expiry, strike, direction, entry_price, quantity,
                original_sl, original_t1, original_t2, original_t3, entry_timestamp,
                signal_reference, registered_at, registered_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                position_id, plan["symbol"], plan.get("expiry"), plan["strike"], plan["direction"],
                plan["entry_price"], plan["quantity"], plan["original_sl"], plan["original_t1"],
                plan.get("original_t2"), plan.get("original_t3"), plan["entry_timestamp"],
                plan.get("signal_reference"), _now(), plan.get("registered_by"),
            ),
        )
        conn.commit()
        return position_id
    finally:
        conn.close()


def get_plan(position_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM trade_guardian_plan WHERE position_id=?", (position_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_plans() -> list:
    conn = _connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM trade_guardian_plan ORDER BY registered_at").fetchall()]
    finally:
        conn.close()


def get_state(position_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM trade_guardian_state WHERE position_id=?", (position_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_state(state: dict) -> None:
    conn = _connect()
    try:
        conn.execute(
            """INSERT INTO trade_guardian_state (
                position_id, state, smart_sl, smart_target_low, smart_target_high,
                breakout_target, trade_health_score, trade_health_tier, action, reason, last_updated
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(position_id) DO UPDATE SET
                state=excluded.state, smart_sl=excluded.smart_sl,
                smart_target_low=excluded.smart_target_low, smart_target_high=excluded.smart_target_high,
                breakout_target=excluded.breakout_target, trade_health_score=excluded.trade_health_score,
                trade_health_tier=excluded.trade_health_tier, action=excluded.action,
                reason=excluded.reason, last_updated=excluded.last_updated""",
            (
                state["position_id"], state["state"], state.get("smart_sl"), state.get("smart_target_low"),
                state.get("smart_target_high"), state.get("breakout_target"), state.get("trade_health_score"),
                state.get("trade_health_tier"), state.get("action"), state.get("reason"), _now(),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def list_states(*, active_only: bool = False) -> list:
    conn = _connect()
    try:
        query = "SELECT * FROM trade_guardian_state"
        if active_only:
            query += " WHERE state NOT IN ('EXIT / THESIS INVALIDATED', 'MANUAL_EXIT', 'TARGET_HIT', 'STOPPED')"
        return [dict(r) for r in conn.execute(query + " ORDER BY last_updated DESC").fetchall()]
    finally:
        conn.close()


def log_decision(entry: dict) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            """INSERT INTO trade_guardian_decision_log (
                position_id, ts, state, underlying_ltp, current_premium, smart_sl, smart_target_low,
                smart_target_high, breakout_target, trade_health_score, trade_health_tier, action, reason,
                component_scores_json, target_feasibility_json, data_quality_json, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                entry["position_id"], _now(), entry.get("state"), entry.get("underlying_ltp"),
                entry.get("current_premium"), entry.get("smart_sl"), entry.get("smart_target_low"),
                entry.get("smart_target_high"), entry.get("breakout_target"), entry.get("trade_health_score"),
                entry.get("trade_health_tier"), entry.get("action"), entry.get("reason"),
                json.dumps(entry.get("component_scores") or {}), json.dumps(entry.get("target_feasibility") or {}),
                json.dumps(entry.get("data_quality") or {}), entry.get("error"),
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def recent_decisions(position_id: str, *, limit: int = 20) -> list:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM trade_guardian_decision_log WHERE position_id=? ORDER BY id DESC LIMIT ?",
            (position_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
