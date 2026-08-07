"""
agents/trading_intelligence/ti_store.py -- SQLite persistence for
Milestone 10's own new state: paper trades and the full signal log
(every AI Trading Engine recommendation, including NO_TRADE/HOLD, for
explainability). Same module-level DB_PATH + per-call connect/close +
PRAGMA busy_timeout convention every prior agent store module already
established, indexed from the start.

A NEW table (`ti_paper_trades`), not a reuse of app.py's existing
paper_trades/scalp_paper_trades/v3_paper_trades/paper_orders -- those
belong to the swing/scalp/V3 engines app.py already owns and manages;
this is a distinct engine with its own trade lifecycle, matching how
each of THOSE engines already got its own dedicated table rather than
sharing one. Writes are pure INSERT/UPDATE, entry price always supplied
by the caller (never fetched live here) -- the exact same safe pattern
app.py's own db_open_paper_trade() already establishes (see package
__init__.py's own safety rule).
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
    conn = _connect()
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ti_paper_trades (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol         TEXT NOT NULL,
                strike         REAL,
                direction      TEXT NOT NULL,
                entry_price    REAL NOT NULL,
                target_price   REAL,
                sl_price       REAL,
                qty            INTEGER NOT NULL DEFAULT 1,
                confidence     INTEGER,
                probability    REAL,
                risk_score     INTEGER,
                reasoning      TEXT,
                entry_time     TEXT NOT NULL,
                exit_price     REAL,
                exit_time      TEXT,
                exit_reason    TEXT,
                points         REAL,
                status         TEXT NOT NULL DEFAULT 'OPEN'
            );

            CREATE TABLE IF NOT EXISTS ti_signal_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                ts             TEXT NOT NULL,
                symbol         TEXT NOT NULL,
                action         TEXT NOT NULL,
                direction      TEXT,
                confidence     INTEGER,
                probability    REAL,
                risk_score     INTEGER,
                entry_price    REAL,
                sl_price       REAL,
                target_price   REAL,
                reasoning      TEXT,
                findings_json  TEXT,
                paper_trade_id INTEGER
            );

            CREATE INDEX IF NOT EXISTS idx_ti_paper_trades_symbol_status ON ti_paper_trades(symbol, status);
            CREATE INDEX IF NOT EXISTS idx_ti_signal_log_symbol_ts ON ti_signal_log(symbol, ts);
            """
        )
        conn.commit()
    finally:
        conn.close()


def open_trade(*, symbol: str, strike: float | None, direction: str, entry_price: float,
                target_price: float | None, sl_price: float | None, qty: int = 1,
                confidence: int | None = None, probability: float | None = None,
                risk_score: int | None = None, reasoning: str | None = None) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO ti_paper_trades (symbol, strike, direction, entry_price, target_price, sl_price, "
            "qty, confidence, probability, risk_score, reasoning, entry_time, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN')",
            (symbol, strike, direction, entry_price, target_price, sl_price, qty,
             confidence, probability, risk_score, reasoning, _now()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def close_trade(trade_id: int, *, exit_price: float, exit_reason: str) -> dict:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM ti_paper_trades WHERE id=?", (trade_id,)).fetchone()
        if row is None:
            raise ValueError(f"no ti_paper_trades row with id={trade_id}")
        # Options are always bought long here (BUY CE / BUY PE -- see
        # ai_trading_engine.py's own module docstring: this framework
        # never writes/sells options), so P&L is simply
        # (exit_premium - entry_premium) * qty -- no direction sign
        # needed, unlike underlying-terms P&L where CE/PE move opposite
        # ways for the same price move.
        points = round((exit_price - row["entry_price"]) * row["qty"], 2)
        conn.execute(
            "UPDATE ti_paper_trades SET exit_price=?, exit_time=?, exit_reason=?, points=?, status='CLOSED' "
            "WHERE id=?",
            (exit_price, _now(), exit_reason, points, trade_id),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM ti_paper_trades WHERE id=?", (trade_id,)).fetchone())
    finally:
        conn.close()


def list_open_trades(*, symbol: str | None = None) -> list:
    conn = _connect()
    try:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM ti_paper_trades WHERE status='OPEN' AND symbol=? ORDER BY entry_time", (symbol,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM ti_paper_trades WHERE status='OPEN' ORDER BY entry_time").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def list_closed_trades(*, symbol: str | None = None, limit: int = 100) -> list:
    conn = _connect()
    try:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM ti_paper_trades WHERE status='CLOSED' AND symbol=? ORDER BY exit_time DESC LIMIT ?",
                (symbol, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM ti_paper_trades WHERE status='CLOSED' ORDER BY exit_time DESC LIMIT ?", (limit,)
            ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def record_signal(*, symbol: str, action: str, direction: str | None = None, confidence: int | None = None,
                   probability: float | None = None, risk_score: int | None = None,
                   entry_price: float | None = None, sl_price: float | None = None,
                   target_price: float | None = None, reasoning: str | None = None,
                   findings: list | None = None, paper_trade_id: int | None = None) -> int:
    conn = _connect()
    try:
        cur = conn.execute(
            "INSERT INTO ti_signal_log (ts, symbol, action, direction, confidence, probability, risk_score, "
            "entry_price, sl_price, target_price, reasoning, findings_json, paper_trade_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_now(), symbol, action, direction, confidence, probability, risk_score, entry_price, sl_price,
             target_price, reasoning, json.dumps(findings) if findings is not None else None, paper_trade_id),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_signals(*, symbol: str | None = None, limit: int = 50) -> list:
    conn = _connect()
    try:
        if symbol:
            rows = conn.execute(
                "SELECT * FROM ti_signal_log WHERE symbol=? ORDER BY ts DESC LIMIT ?", (symbol, limit)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM ti_signal_log ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["findings_json"] = json.loads(d["findings_json"]) if d.get("findings_json") else None
        out.append(d)
    return out
