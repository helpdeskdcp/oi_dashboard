"""
agents/shadow_mode/store.py -- Milestone 12, Phase 2B: Shadow Mode's own
isolated table namespace (shadow_observations, shadow_predictions,
shadow_outcomes). CREATE TABLE IF NOT EXISTS only, no migration of any
existing table, no write anywhere else in this database -- mirrors the
exact DB_PATH/_connect()/init_db() shape agents/runtime/runtime_store.py
already established for a brand-new module's tables.

Three tables, one row-per-event each (append-only -- nothing here is
ever UPDATEd, only INSERTed, so a partially-evaluated prediction is
never at risk of being silently overwritten):

- shadow_observations: what the pipeline saw at one point in time
  (underlying LTP, ATM, PCR, bias) -- the raw market snapshot a
  prediction was derived from.
- shadow_predictions: the hypothetical trade decision derived from one
  observation (signal_type, confidence, expected direction/target
  range, reasoning). Recording a prediction NEVER touches paper_orders,
  paper_trades, or ti_paper_trades -- there is no execution path from
  this table to any of those.
- shadow_outcomes: filled in later, by evaluator.py, once enough
  archived candle data exists after a prediction's timestamp to judge
  it. At most one outcome per prediction (UNIQUE prediction_id).
"""
import sqlite3

DB_PATH = "oi_history.db"


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
            CREATE TABLE IF NOT EXISTS shadow_observations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              TEXT NOT NULL,
                symbol          TEXT NOT NULL,
                timeframe       TEXT NOT NULL,
                underlying_ltp  REAL,
                atm             INTEGER,
                pcr             REAL,
                market_bias     TEXT,
                bias_note       TEXT,
                context_json    TEXT
            );

            CREATE TABLE IF NOT EXISTS shadow_predictions (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                observation_id        INTEGER NOT NULL,
                ts                    TEXT NOT NULL,
                symbol                TEXT NOT NULL,
                timeframe             TEXT NOT NULL,
                signal_type           TEXT NOT NULL,
                expected_direction    TEXT,
                confidence            INTEGER,
                reasoning_snapshot    TEXT,
                entry_reference_price REAL,
                expected_target_low   REAL,
                expected_target_high  REAL,
                valid_until_ts        TEXT,
                FOREIGN KEY(observation_id) REFERENCES shadow_observations(id)
            );

            CREATE TABLE IF NOT EXISTS shadow_outcomes (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                prediction_id     INTEGER NOT NULL UNIQUE,
                evaluated_ts      TEXT NOT NULL,
                actual_direction  TEXT,
                actual_move_pts   REAL,
                actual_move_pct   REAL,
                classification    TEXT NOT NULL,
                notes             TEXT,
                FOREIGN KEY(prediction_id) REFERENCES shadow_predictions(id)
            );

            CREATE INDEX IF NOT EXISTS idx_shadow_predictions_symbol_ts
                ON shadow_predictions(symbol, ts);
            CREATE INDEX IF NOT EXISTS idx_shadow_predictions_observation_id
                ON shadow_predictions(observation_id);
            CREATE INDEX IF NOT EXISTS idx_shadow_outcomes_prediction_id
                ON shadow_outcomes(prediction_id);
            """
        )
        conn.commit()
    finally:
        conn.close()


def record_observation(*, ts, symbol, timeframe, underlying_ltp=None, atm=None, pcr=None,
                        market_bias=None, bias_note=None, context_json=None) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO shadow_observations "
            "(ts, symbol, timeframe, underlying_ltp, atm, pcr, market_bias, bias_note, context_json) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (ts, symbol, timeframe, underlying_ltp, atm, pcr, market_bias, bias_note, context_json),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def record_prediction(*, observation_id, ts, symbol, timeframe, signal_type, expected_direction=None,
                       confidence=None, reasoning_snapshot=None, entry_reference_price=None,
                       expected_target_low=None, expected_target_high=None, valid_until_ts=None) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO shadow_predictions "
            "(observation_id, ts, symbol, timeframe, signal_type, expected_direction, confidence, "
            " reasoning_snapshot, entry_reference_price, expected_target_low, expected_target_high, valid_until_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (observation_id, ts, symbol, timeframe, signal_type, expected_direction, confidence,
             reasoning_snapshot, entry_reference_price, expected_target_low, expected_target_high, valid_until_ts),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def record_outcome(*, prediction_id, evaluated_ts, classification, actual_direction=None,
                    actual_move_pts=None, actual_move_pct=None, notes=None) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO shadow_outcomes "
            "(prediction_id, evaluated_ts, actual_direction, actual_move_pts, actual_move_pct, classification, notes) "
            "VALUES (?,?,?,?,?,?,?)",
            (prediction_id, evaluated_ts, actual_direction, actual_move_pts, actual_move_pct, classification, notes),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_prediction(prediction_id) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM shadow_predictions WHERE id=?", (prediction_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def get_outcome_for_prediction(prediction_id) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM shadow_outcomes WHERE prediction_id=?", (prediction_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_predictions_pending_evaluation(*, limit: int = 100) -> list:
    """Predictions with no shadow_outcomes row yet, oldest first --
    what evaluator.evaluate_pending() works through."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT p.* FROM shadow_predictions p "
            "LEFT JOIN shadow_outcomes o ON o.prediction_id = p.id "
            "WHERE o.id IS NULL ORDER BY p.ts ASC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def count_observations() -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM shadow_observations").fetchone()[0]
    finally:
        conn.close()


def count_predictions() -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM shadow_predictions").fetchone()[0]
    finally:
        conn.close()


def count_observations_since(since_ts: str) -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM shadow_observations WHERE ts >= ?", (since_ts,)).fetchone()[0]
    finally:
        conn.close()


def count_predictions_since(since_ts: str) -> int:
    conn = _connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM shadow_predictions WHERE ts >= ?", (since_ts,)).fetchone()[0]
    finally:
        conn.close()


def count_evaluated_outcomes_since(since_ts: str) -> int:
    """Outcomes EVALUATED since `since_ts` (filters on shadow_outcomes.
    evaluated_ts, not the underlying prediction's own ts) -- "how many
    grading decisions happened today," distinct from "how many
    predictions were made today.\""""
    conn = _connect()
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM shadow_outcomes WHERE evaluated_ts >= ?", (since_ts,)
        ).fetchone()[0]
    finally:
        conn.close()


def last_prediction_ts() -> str | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT ts FROM shadow_predictions ORDER BY ts DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    return row["ts"] if row else None


def list_recent_predictions_with_outcomes(*, symbol: str | None = None, limit: int = 10) -> list:
    """Newest-first predictions LEFT JOINed with their outcome (None if
    not yet evaluated) -- the dashboard's "last N observations" table
    and api.get_recent()'s own data source."""
    conn = _connect()
    try:
        sql = (
            "SELECT p.*, o.classification, o.actual_direction, o.actual_move_pts, o.actual_move_pct, "
            "       o.evaluated_ts "
            "FROM shadow_predictions p LEFT JOIN shadow_outcomes o ON o.prediction_id = p.id "
        )
        params: list = []
        if symbol:
            sql += "WHERE p.symbol = ? "
            params.append(symbol)
        sql += "ORDER BY p.ts DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def list_evaluated_predictions(*, symbol: str | None = None, since_ts: str | None = None) -> list:
    """Every prediction that HAS an outcome -- compute_metrics()'s own
    data source."""
    conn = _connect()
    try:
        sql = (
            "SELECT p.*, o.classification, o.actual_direction, o.actual_move_pts, o.actual_move_pct "
            "FROM shadow_predictions p JOIN shadow_outcomes o ON o.prediction_id = p.id "
        )
        clauses, params = [], []
        if symbol:
            clauses.append("p.symbol = ?")
            params.append(symbol)
        if since_ts:
            clauses.append("p.ts >= ?")
            params.append(since_ts)
        if clauses:
            sql += "WHERE " + " AND ".join(clauses) + " "
        sql += "ORDER BY p.ts DESC"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]
