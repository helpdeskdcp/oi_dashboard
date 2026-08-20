"""
agents/trading_intelligence/execution_state.py -- Post-launch upgrade,
Phase A: the autonomous-execution STATE MACHINE and its own persistence
layer. ADVISORY / PERSISTED-ONLY -- this module contains no broker call
of any kind (verified by this package's own AST safety scan,
test_agents/trading_intelligence/test_safety.py, exactly like every
other module here) and cannot place, modify, or cancel a real order.
See broker_execution.py's own module docstring for the (also disabled)
adapter interface this state machine will eventually drive.

STATE MACHINE:

    SIGNAL -> APPROVED -> READY -> ORDER_INTENT -> SUBMITTED -> FILLED
    -> MONITORING -[TARGET_UPDATE|SL_UPDATE|TRAILING]-> MONITORING
    -> EXIT_INTENT -> EXIT -> COMPLETED

MONITORING is the hub state: from it, TARGET_UPDATE/SL_UPDATE/TRAILING/
EXIT_INTENT are all reachable, and the first three return to MONITORING
once applied (the ongoing "recalculate and adjust" loop Trade Guardian
already performs, once a future phase wires this state machine to it).
COMPLETED is terminal -- no transition is ever valid out of it.

IDEMPOTENCY / DUPLICATE PROTECTION -- the two properties this whole
module exists to guarantee before any real broker adapter is ever
wired in:
  1. create_execution() is idempotent on execution_id: a second call
     with the same id returns the EXISTING record untouched, never a
     second row -- the same "immutable once created" pattern
     trade_guardian_store.register_plan() already established. This is
     what makes a duplicate signal/retry structurally incapable of
     creating a second, independent execution lifecycle.
  2. transition() treats a request to move to the CURRENT state as a
     successful no-op (not an error) -- a retried "move to X" call that
     already succeeded once is idempotent, not a spurious rejection.
  3. The state graph itself only allows ORDER_INTENT/SUBMITTED to be
     reached ONCE per execution_id, in strict sequence -- there is no
     path back to ORDER_INTENT from anywhere downstream of it, so a
     future real adapter can never be asked to submit a second order
     for the same execution_id.

Every transition -- valid or REJECTED -- is written to
execution_transition_log (append-only), so an invalid-transition
attempt is itself part of the permanent audit trail, never silently
dropped.
"""
import datetime as dt
import json
import sqlite3

from .. import timekeeping

DB_PATH = "oi_history.db"

STATES = (
    "SIGNAL", "APPROVED", "READY", "ORDER_INTENT", "SUBMITTED", "FILLED", "MONITORING",
    "TARGET_UPDATE", "SL_UPDATE", "TRAILING", "EXIT_INTENT", "EXIT", "COMPLETED",
)

# The one place the transition graph is defined -- every caller (this
# module's own transition(), any future UI/CLI, any future scheduler
# wiring) goes through this, never a second copy of the graph.
VALID_TRANSITIONS = {
    "SIGNAL": frozenset({"APPROVED"}),
    "APPROVED": frozenset({"READY"}),
    "READY": frozenset({"ORDER_INTENT"}),
    "ORDER_INTENT": frozenset({"SUBMITTED"}),
    "SUBMITTED": frozenset({"FILLED"}),
    "FILLED": frozenset({"MONITORING"}),
    "MONITORING": frozenset({"TARGET_UPDATE", "SL_UPDATE", "TRAILING", "EXIT_INTENT"}),
    "TARGET_UPDATE": frozenset({"MONITORING"}),
    "SL_UPDATE": frozenset({"MONITORING"}),
    "TRAILING": frozenset({"MONITORING"}),
    "EXIT_INTENT": frozenset({"EXIT"}),
    "EXIT": frozenset({"COMPLETED"}),
    "COMPLETED": frozenset(),  # terminal -- nothing is ever valid out of it
}


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
            CREATE TABLE IF NOT EXISTS execution_state (
                execution_id      TEXT PRIMARY KEY,
                instrument        TEXT NOT NULL,
                strike            REAL,
                direction         TEXT NOT NULL,
                entry_price       REAL,
                quantity          INTEGER,
                sl                REAL,
                t1                REAL,
                t2                REAL,
                t3                REAL,
                current_state     TEXT NOT NULL,
                confidence        REAL,
                decision_reason   TEXT,
                signal_reference  TEXT,
                broker_order_id   TEXT,
                error_status      TEXT,
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS execution_transition_log (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id   TEXT NOT NULL,
                ts             TEXT NOT NULL,
                from_state     TEXT,
                to_state       TEXT NOT NULL,
                accepted       INTEGER NOT NULL,
                reason         TEXT,
                metadata_json  TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_execution_transition_log_execution_ts
                ON execution_transition_log(execution_id, ts);
            """
        )
        # Fix for the expiry-contract-identity bug class (same one PR #30/
        # #32/#33 fixed for every paper-trade table this milestone --
        # Codex review finding, MEDIUM): list_executions_with_live_ltp()
        # reads the most recent strikes-table reading for (instrument,
        # strike) with no check that it's still the SAME option contract
        # this execution was created against. Once that contract's own
        # expiry date passes, ANY strikes-table reading for that strike
        # number necessarily belongs to a different, freshly-priced
        # instrument. Self-migrating guarded ALTER, same pattern every
        # other table in this fix class already uses.
        existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(execution_state)")}
        if "expiry_date_at_entry" not in existing_cols:
            conn.execute("ALTER TABLE execution_state ADD COLUMN expiry_date_at_entry TEXT")
        conn.commit()
    finally:
        conn.close()


def create_execution(execution_id: str, *, instrument: str, direction: str, strike: float | None = None,
                      entry_price: float | None = None, quantity: int | None = None, sl: float | None = None,
                      t1: float | None = None, t2: float | None = None, t3: float | None = None,
                      confidence: float | None = None, decision_reason: str | None = None,
                      signal_reference: str | None = None, expiry_date: str | None = None) -> dict:
    """Creates a new execution record in state SIGNAL. IDEMPOTENT: if
    execution_id already exists, the existing row is returned
    completely untouched -- never overwritten, never duplicated. This
    is the structural guarantee behind "duplicate execution intents
    must never create duplicate orders": the SAME execution_id can only
    ever have ONE row, ONE lifecycle.

    expiry_date: this execution's own option contract's expiry (ISO date
    string, e.g. the same symbol_expiry api.run_scheduled_cycle() already
    resolved this cycle) -- what list_executions_with_live_ltp() checks
    before trusting a strikes-table reading for (instrument, strike)
    actually belongs to THIS contract, not one that rolled over since.
    None is honest ("expiry unknown at creation time"), never fabricated."""
    if direction not in ("CE", "PE"):
        raise ValueError(f"direction must be 'CE' or 'PE', got {direction!r}")
    conn = _connect()
    try:
        existing = conn.execute(
            "SELECT * FROM execution_state WHERE execution_id=?", (execution_id,)
        ).fetchone()
        if existing:
            return dict(existing)
        now = _now()
        conn.execute(
            """INSERT INTO execution_state (
                execution_id, instrument, strike, direction, entry_price, quantity, sl, t1, t2, t3,
                current_state, confidence, decision_reason, signal_reference, broker_order_id, error_status,
                created_at, updated_at, expiry_date_at_entry
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SIGNAL', ?, ?, ?, NULL, NULL, ?, ?, ?)""",
            (
                execution_id, instrument, strike, direction, entry_price, quantity, sl, t1, t2, t3,
                confidence, decision_reason, signal_reference, now, now, expiry_date,
            ),
        )
        conn.execute(
            """INSERT INTO execution_transition_log (
                execution_id, ts, from_state, to_state, accepted, reason, metadata_json
            ) VALUES (?, ?, NULL, 'SIGNAL', 1, 'execution created', ?)""",
            (execution_id, now, json.dumps({"instrument": instrument, "direction": direction})),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM execution_state WHERE execution_id=?", (execution_id,)).fetchone())
    finally:
        conn.close()


def get_execution(execution_id: str) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM execution_state WHERE execution_id=?", (execution_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_executions(*, active_only: bool = False) -> list:
    conn = _connect()
    try:
        query = "SELECT * FROM execution_state"
        if active_only:
            query += " WHERE current_state NOT IN ('COMPLETED')"
        return [dict(r) for r in conn.execute(query + " ORDER BY updated_at DESC").fetchall()]
    finally:
        conn.close()


def list_executions_with_live_ltp(*, active_only: bool = False) -> list:
    """list_executions() enriched with each NON-COMPLETED execution's
    current live LTP and a derived hit_status (TARGET_HIT/SL_HIT/ACTIVE),
    read from data_access.recent_strike_history() -- the SAME already-
    logged cycles/strikes data every other panel on this page reads.
    Never a new broker call, never any new load on Angel One's rate limit.

    Expiry-contract-identity check (Codex review finding, MEDIUM, fixed
    2026-08-20): the same bug class PR #30/#32/#33 fixed for every
    paper-trade table applies here too -- a strikes-table reading for
    (instrument, strike) says nothing about WHICH option contract it
    belongs to. cycles/strikes carry no expiry column at all, so once
    this execution's own expiry_date_at_entry has passed, ANY current
    reading for that strike number necessarily belongs to a different,
    freshly-priced instrument (the strike ladder gets reused every
    expiry cycle). live_ltp/hit_status are therefore only ever computed
    while expiry_date_at_entry is known AND still >= today; expired or
    unknown-expiry executions get None/None, same honest "can't verify"
    contract as a missing strikes reading -- never a guess.

    COMPLETED executions deliberately get live_ltp=None/hit_status=None:
    that shadow lifecycle already resolved via the real, authoritative
    paper-trading outcome (ti_paper_trades.exit_reason) -- computing a
    fresh "live" hit_status against CURRENT price for an already-closed
    execution would be actively misleading (e.g. showing ACTIVE for a
    trade that's actually done).

    Purely informational -- this NEVER calls transition() or writes
    anything; the real current_state is untouched by this function."""
    from . import data_access

    today = timekeeping.now_ist().date()
    executions = list_executions(active_only=active_only)
    for e in executions:
        e["live_ltp"] = None
        e["hit_status"] = None
        if e["current_state"] == "COMPLETED" or not e.get("strike") or not e.get("direction"):
            continue
        expiry_at_entry = e.get("expiry_date_at_entry")
        if not expiry_at_entry:
            continue   # expiry unknown at creation time -- can't verify contract identity, don't guess
        try:
            expiry_date_obj = dt.date.fromisoformat(expiry_at_entry)
        except ValueError:
            continue   # malformed value -- fail closed, never crash the whole panel over one bad row
        if expiry_date_obj < today:
            continue   # this contract has expired -- any current strikes-table reading is a different instrument
        history = data_access.recent_strike_history(e["instrument"], int(e["strike"]), limit=1)
        if not history:
            continue
        ltp = history[0]["ce_ltp"] if e["direction"] == "CE" else history[0]["pe_ltp"]
        if not ltp:
            continue
        e["live_ltp"] = ltp
        if e.get("t1") is not None and ltp >= e["t1"]:
            e["hit_status"] = "TARGET_HIT"
        elif e.get("sl") is not None and ltp <= e["sl"]:
            e["hit_status"] = "SL_HIT"
        else:
            e["hit_status"] = "ACTIVE"
    return executions


def recent_transitions(execution_id: str, *, limit: int = 50) -> list:
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM execution_transition_log WHERE execution_id=? ORDER BY id DESC LIMIT ?",
            (execution_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def transition(execution_id: str, to_state: str, *, reason: str | None = None, metadata: dict | None = None) -> dict:
    """The ONLY way execution_state.current_state ever changes.
    Deterministic (pure lookup against VALID_TRANSITIONS), idempotent
    (a request to move to the state already occupied succeeds as a
    no-op, never an error), restart-safe (every check reads the
    DB-persisted current state fresh, never an in-memory cache), and
    auditable (every attempt -- accepted or rejected -- is appended to
    execution_transition_log, never silently dropped).

    Returns {"ok": bool, "execution_id": ..., "from_state": ...,
    "to_state": ..., "reason": ...} -- never raises for an invalid
    transition or an unknown execution_id (both are honest, logged
    rejections, matching every other advisory module's contract in
    this package).

    AUDIT CONTRACT (fixed after PR #17 review found a gap): every
    deterministic rejection path -- unknown execution_id, unknown
    state name, invalid transition, a transition attempted out of the
    terminal COMPLETED state -- writes an execution_transition_log row
    BEFORE returning, exactly like the accepted-transition path
    already did. execution_transition_log.execution_id carries no
    foreign-key constraint against execution_state, so a rejection can
    be logged even for an execution_id that was never created --
    "preserve the rejection audit record even though there is no
    existing execution_state row" is satisfied structurally, not by a
    special case."""
    conn = _connect()
    try:
        now = _now()
        row = conn.execute("SELECT current_state FROM execution_state WHERE execution_id=?", (execution_id,)).fetchone()
        current_state = row["current_state"] if row is not None else None

        def _log(accepted: bool, why: str) -> None:
            conn.execute(
                """INSERT INTO execution_transition_log (
                    execution_id, ts, from_state, to_state, accepted, reason, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (execution_id, now, current_state, to_state, int(accepted), why, json.dumps(metadata or {})),
            )
            conn.commit()

        if to_state not in STATES:
            why = f"{to_state!r} is not a valid state"
            _log(False, why)
            return {"ok": False, "execution_id": execution_id, "from_state": current_state, "to_state": to_state, "reason": why}

        if row is None:
            why = "no execution record found for this execution_id"
            _log(False, why)
            return {"ok": False, "execution_id": execution_id, "from_state": None, "to_state": to_state, "reason": why}

        if to_state == current_state:
            why = reason or "idempotent no-op -- already in this state"
            _log(True, why)
            return {"ok": True, "execution_id": execution_id, "from_state": current_state, "to_state": to_state, "reason": why}

        allowed = VALID_TRANSITIONS.get(current_state, frozenset())
        if to_state not in allowed:
            why = f"invalid transition: {current_state} -> {to_state} is not allowed"
            _log(False, why)
            return {"ok": False, "execution_id": execution_id, "from_state": current_state, "to_state": to_state, "reason": why}

        conn.execute(
            "UPDATE execution_state SET current_state=?, updated_at=? WHERE execution_id=?",
            (to_state, now, execution_id),
        )
        _log(True, reason)
        return {"ok": True, "execution_id": execution_id, "from_state": current_state, "to_state": to_state, "reason": reason}
    finally:
        conn.close()


def set_error_status(execution_id: str, error_status: str) -> None:
    """Records an error/status note on the execution row without
    changing current_state -- e.g. a rejected order or a broker-side
    failure a future adapter reports. Purely descriptive; the state
    machine itself is unaffected (the caller decides separately, via
    transition(), whether the error also warrants a state change)."""
    conn = _connect()
    try:
        conn.execute(
            "UPDATE execution_state SET error_status=?, updated_at=? WHERE execution_id=?",
            (error_status, _now(), execution_id),
        )
        conn.commit()
    finally:
        conn.close()
