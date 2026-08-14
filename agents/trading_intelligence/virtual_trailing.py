"""
agents/trading_intelligence/virtual_trailing.py -- Milestone 21, Phase 1:
the Virtual Trailing Engine. Paper-trade / advisory ONLY -- this module
never calls ti_store.close_trade(), never mutates a real ti_paper_trades
row, and (per this package's own __init__.py safety rule, verified by
test_safety.py's AST scan) never touches a broker in any form.

Motivation (the real incident that started this milestone): a real
98%-confidence SILVER structure alert hit its first fixed target (T1)
and kept running well past it, but the paper-trading engine's own exit
logic (ai_trading_engine._check_open_trade_exit()) only ever knows how
to close a trade at its ORIGINAL fixed target_price/sl_price -- it has
no concept of protecting a big move once it's underway. This module is
a separate, SHADOW state machine layered on top of the real trade
lifecycle: for every real OPEN paper trade it tracks a parallel
"what if we trailed instead of taking a fixed target" position, purely
for observation. The real trade's own fixed-target exit is completely
unaffected and keeps running exactly as before.

Trailing rules (as specified):
    +8  pts from entry  -> move virtual SL to cost (breakeven)
    +12 pts from entry  -> trail by 4
    +20 pts from entry  -> trail by 6
    +30 pts from entry  -> trail by 10 , and enter RUNNER MODE (the
                            virtual target is dropped -- once the
                            deepest defined tier is reached, this
                            engine stops capping the winner at a fixed
                            target and just keeps trailing the peak)
    Exit (virtual only) when premium falls back from the highest
    premium achieved by the currently active trail amount.

ATR-based trailing: once a trail amount applies (the +12/+20/+30
tiers), it is never tighter than ATR_TRAIL_FLOOR_MULTIPLIER * the
symbol's own atr_14 (already-computed, read via
data_access.latest_market_structure() -- never recomputed here) --
so ordinary volatility noise for a genuinely volatile symbol can't
trigger a premature virtual exit. The breakeven tier (+8) is not
ATR-scaled -- it is always exactly the entry price, a fixed capital-
protection floor.

Capital protection: the virtual SL only ever moves up, never down,
once the breakeven tier is reached -- a pullback that doesn't fully
reverse the move can never push the recorded floor back below a
previously-locked-in level.

State is read via the SAME already-stored-cycle data every other
advisory read in this package already uses
(data_access.recent_strike_history()) -- never a live fetch, never a
new broker call. A trade whose real position has already closed simply
stops appearing in ti_store.list_open_trades() on a later cycle; its
virtual_trailing_state row is left exactly as last computed (frozen),
the same way every other shadow/advisory layer in this package behaves.
"""
import datetime as dt
import sqlite3

from . import data_access, ti_store

DB_PATH = "oi_history.db"

BREAKEVEN_TRIGGER_POINTS = 8
TRAIL_TIERS = ((30, 10), (20, 6), (12, 4))   # (min gain to use this tier, trail amount) -- checked highest-first
RUNNER_MODE_TRIGGER_POINTS = TRAIL_TIERS[0][0]   # the deepest defined tier
ATR_TRAIL_FLOOR_MULTIPLIER = 0.5   # a trail amount is never tighter than this * atr_14


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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS virtual_trailing_state (
                trade_id          INTEGER PRIMARY KEY,
                symbol            TEXT NOT NULL,
                direction         TEXT NOT NULL,
                entry_price       REAL NOT NULL,
                original_sl_price REAL,
                highest_premium   REAL NOT NULL,
                virtual_sl        REAL,
                virtual_target    REAL,
                runner_mode       INTEGER NOT NULL DEFAULT 0,
                locked_profit     REAL NOT NULL DEFAULT 0,
                state             TEXT NOT NULL DEFAULT 'ACTIVE',
                exit_price        REAL,
                exit_reason       TEXT,
                created_ts        TEXT NOT NULL,
                updated_ts        TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_virtual_trailing_state_symbol ON virtual_trailing_state(symbol)")
        conn.commit()
    finally:
        conn.close()


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["runner_mode"] = bool(d["runner_mode"])
    return d


def get_state(trade_id: int) -> dict | None:
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM virtual_trailing_state WHERE trade_id=?", (trade_id,)).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row is not None else None


def list_states(*, symbol: str | None = None, active_only: bool = False) -> list:
    conn = _connect()
    try:
        clauses, params = [], []
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol)
        if active_only:
            clauses.append("state='ACTIVE'")
        query = "SELECT * FROM virtual_trailing_state"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY updated_ts DESC"
        rows = conn.execute(query, params).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def upsert_state(state: dict) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO virtual_trailing_state (trade_id, symbol, direction, entry_price, original_sl_price, "
            "highest_premium, virtual_sl, virtual_target, runner_mode, locked_profit, state, exit_price, "
            "exit_reason, created_ts, updated_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(trade_id) DO UPDATE SET highest_premium=excluded.highest_premium, "
            "virtual_sl=excluded.virtual_sl, virtual_target=excluded.virtual_target, "
            "runner_mode=excluded.runner_mode, locked_profit=excluded.locked_profit, state=excluded.state, "
            "exit_price=excluded.exit_price, exit_reason=excluded.exit_reason, updated_ts=excluded.updated_ts",
            (state["trade_id"], state["symbol"], state["direction"], state["entry_price"],
             state["original_sl_price"], state["highest_premium"], state["virtual_sl"], state["virtual_target"],
             int(state["runner_mode"]), state["locked_profit"], state["state"], state["exit_price"],
             state["exit_reason"], state["created_ts"], state["updated_ts"]),
        )
        conn.commit()
    finally:
        conn.close()


def _init_state(trade: dict, *, now: str) -> dict:
    return {
        "trade_id": trade["id"], "symbol": trade["symbol"], "direction": trade["direction"],
        "entry_price": trade["entry_price"], "original_sl_price": trade.get("sl_price"),
        "highest_premium": trade["entry_price"], "virtual_sl": trade.get("sl_price"),
        "virtual_target": trade.get("target_price"), "runner_mode": False, "locked_profit": 0.0,
        "state": "ACTIVE", "exit_price": None, "exit_reason": None,
        "created_ts": now, "updated_ts": now,
    }


def _candidate_trail_amount(gain: float, atr_14: float | None) -> float | None:
    """None below the +12 tier (the breakeven-only tier and below have no
    ATR-scaled trail amount of their own)."""
    for threshold, amount in TRAIL_TIERS:
        if gain >= threshold:
            if atr_14:
                return max(amount, round(atr_14 * ATR_TRAIL_FLOOR_MULTIPLIER, 2))
            return amount
    return None


def evaluate_trade(state: dict, current_premium: float, *, atr_14: float | None = None) -> dict:
    """Pure function -- given the prior virtual_trailing_state row and
    this cycle's current premium, returns the NEW state. Never mutates
    `state` in place. A row already EXITED is returned unchanged (frozen
    -- once virtually closed, this engine stops evaluating it, exactly
    like a real closed trade)."""
    new = dict(state)
    if new["state"] != "ACTIVE":
        return new

    entry = new["entry_price"]
    new["highest_premium"] = round(max(new["highest_premium"], current_premium), 2)
    gain = round(new["highest_premium"] - entry, 2)

    if gain >= BREAKEVEN_TRIGGER_POINTS:
        trail_amount = _candidate_trail_amount(gain, atr_14)
        candidate_sl = entry if trail_amount is None else round(new["highest_premium"] - trail_amount, 2)
        floor = new["virtual_sl"] if new["virtual_sl"] is not None else entry
        # capital protection + trailing invariant: the virtual SL only ever moves up, never back down
        new["virtual_sl"] = round(max(floor, candidate_sl, entry), 2)
        if gain >= RUNNER_MODE_TRIGGER_POINTS:
            new["runner_mode"] = True
            new["virtual_target"] = None   # uncapped -- keep trailing the peak instead of taking a fixed target
    # below the breakeven trigger, virtual_sl stays at the trade's own original_sl_price (untouched)

    new["locked_profit"] = round(max(0.0, (new["virtual_sl"] or entry) - entry), 2)

    if new["virtual_sl"] is not None and current_premium <= new["virtual_sl"]:
        new["state"] = "EXITED"
        new["exit_price"] = current_premium
        new["exit_reason"] = "VIRTUAL TRAILING STOP" if gain >= BREAKEVEN_TRIGGER_POINTS else "VIRTUAL STOP LOSS"

    new["updated_ts"] = _now()
    return new


def _current_premium(trade: dict) -> float | None:
    """Same already-stored-cycle data source
    ai_trading_engine._check_open_trade_exit() falls back to -- never a
    live fetch."""
    if not trade.get("strike"):
        return None
    history = data_access.recent_strike_history(trade["symbol"], trade["strike"], limit=1)
    if not history:
        return None
    row = history[0]
    ltp = row["ce_ltp"] if trade["direction"] == "CE" else row["pe_ltp"]
    return ltp if ltp and ltp > 0 else None


def run_virtual_trailing_cycle(symbols: list) -> list:
    """Called once per trading_intelligence cycle, gated by
    config.TI_ENABLE_VIRTUAL_TRAILING. For every real OPEN paper trade
    across `symbols` (ti_store.list_open_trades()), initializes this
    trade's virtual state on first sight, then updates it against the
    current stored premium. Purely additive: never calls
    ti_store.close_trade(), never mutates the real ti_paper_trades row.
    Returns a list of finding dicts (only for trades that just virtually
    exited this cycle) for the caller's own logging/findings list."""
    findings = []
    for symbol in symbols:
        market_structure = data_access.latest_market_structure(symbol)
        atr_14 = market_structure.get("atr_14") if market_structure else None
        for trade in ti_store.list_open_trades(symbol=symbol):
            current_premium = _current_premium(trade)
            if current_premium is None:
                continue
            prior = get_state(trade["id"])
            was_active = prior is None or prior["state"] == "ACTIVE"
            state = prior if prior is not None else _init_state(trade, now=_now())
            new_state = evaluate_trade(state, current_premium, atr_14=atr_14)
            upsert_state(new_state)
            if was_active and new_state["state"] == "EXITED":
                findings.append({
                    "summary": f"{symbol}: virtual trailing exit on trade #{trade['id']} -- "
                               f"{new_state['exit_reason']} at {new_state['exit_price']} "
                               f"(locked {new_state['locked_profit']} pts)",
                    "severity": "info",
                })
    return findings
