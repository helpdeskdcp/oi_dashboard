"""
agents/risk_manager/data_access.py -- the ONLY module in
agents/risk_manager/ that reads live (paper-trading) position/wallet/
Greeks data. Every query here is a plain SELECT against tables app.py
already owns and writes (paper_orders, paper_trades, scalp_paper_trades,
v3_paper_trades, users, cycles, strikes) plus, as of Milestone 25
Workstream 3, agents/trading_intelligence/ti_store.py's own
ti_paper_trades (the Trading Intelligence engine's paper trades -- the
one engine this module used to exclude entirely, a real gap the M25
audit found: the Live Portfolio Risk Monitor could never see a TI
position). Never a live broker API call. In particular this module
NEVER touches the Angel One session object (no .get_option_greeks(), no
live login) -- a documented landmine in this repo (hitting
/live-positions in a test already triggered a real duplicate broker
login once, per this project's own history). Theta/Vega, when available
at all, come from whatever the live app already wrote into the `strikes`
table the last time it polled the broker -- read-only here, never
triggered fresh.

Schema source: paper_orders/paper_trades/scalp_paper_trades/
v3_paper_trades/users/wallet_transactions/cycles/strikes are all defined
in app.py's own init_db(); ti_paper_trades is defined in
agents/trading_intelligence/ti_store.py's own init_db(). This module
never creates or migrates any of them -- only reads.

UNITS WARNING (read before adding a new aggregate here): paper_trades/
scalp_paper_trades/v3_paper_trades/paper_orders all store `points` as a
RAW premium-price difference (exit_price - entry_price), never
multiplied by quantity -- none of those four tables has a real
per-trade qty column (paper_orders is the one exception with a real
`qty`, but its own `points` column is still stored un-multiplied; qty is
applied separately at wallet-credit time, see app.py's own comment at
the paper_orders exit site). ti_paper_trades.points is DIFFERENT: Module
5's ti_store.close_trade() stores `(exit_price - entry_price) * qty`
directly -- already quantity-scaled. Summing raw `points` across the
legacy four tables (as daily_realized_pnl()/closed_trade_points_today()
below already correctly do) is internally consistent; blindly adding
ti_paper_trades to that same SUM would NOT be -- it would silently mix
un-scaled and pre-scaled numbers, exactly the "financially incorrect
normalization" the M25 audit was warned against inventing. See
ti_daily_realized_pnl() below, kept deliberately separate, and
agents/risk_manager/risk_decision.py for how the two get combined
correctly (each converted to rupee terms on its own, then added).
"""
import dataclasses
import sqlite3

DB_PATH = "oi_history.db"

# System-wide (no user_id) paper-trading tables -- each independent
# engine's own open positions, not attributable to a specific user.
# ti_paper_trades is included here for POSITION LISTING (exposure/heat/
# concentration -- see Position.qty_is_estimated below for the honesty
# caveat that composition needs) but deliberately NOT for the points-SUM
# helpers further down (see this module's own UNITS WARNING above).
_ENGINE_TABLES = ("paper_trades", "scalp_paper_trades", "v3_paper_trades", "ti_paper_trades")
_LEGACY_POINTS_TABLES = ("paper_trades", "scalp_paper_trades", "v3_paper_trades")
# Tables with no persisted per-trade quantity -- Position.qty defaults to
# 1 for these (see _row_to_position()), which is an ESTIMATE, not a real
# per-symbol lot size. Callers computing rupee-denominated exposure from
# these positions must disclose that (see risk_decision.py's own
# qty_basis_note).
_QTY_ESTIMATED_TABLES = ("paper_trades", "scalp_paper_trades", "v3_paper_trades")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@dataclasses.dataclass
class Position:
    source: str  # "paper_orders" | "paper_trades" | "scalp_paper_trades" | "v3_paper_trades" | "ti_paper_trades"
    id: int
    user_id: int | None
    symbol: str
    strike: float | None
    direction: str | None  # "CE" | "PE"
    entry_price: float
    sl_price: float | None
    target_price: float | None
    qty: int
    entry_time: str | None
    # True when `qty` is a fallback default (1), not a real persisted
    # per-trade quantity -- paper_trades/scalp_paper_trades/v3_paper_trades
    # have no qty column at all (see this module's own UNITS WARNING).
    # False for paper_orders and ti_paper_trades, both of which store a
    # real per-trade qty.
    qty_is_estimated: bool = False


def _row_to_position(source: str, row: sqlite3.Row) -> Position:
    d = dict(row)
    return Position(
        source=source, id=d.get("id"), user_id=d.get("user_id"), symbol=d.get("symbol"),
        strike=d.get("strike"), direction=d.get("direction"), entry_price=float(d.get("entry_price") or 0.0),
        sl_price=(float(d["sl_price"]) if d.get("sl_price") is not None else None),
        target_price=(float(d["target_price"]) if d.get("target_price") is not None else None),
        qty=int(d.get("qty") or 1), entry_time=d.get("entry_time"),
        qty_is_estimated=(source in _QTY_ESTIMATED_TABLES),
    )


def open_positions_for_user(user_id: int) -> list:
    """Same canonical query the live dashboard's manual-trading page and
    /api/manual-trade/my-trades endpoint already use (app.py) -- results
    match exactly what that user sees on screen."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT * FROM paper_orders WHERE user_id=? AND status='OPEN' ORDER BY entry_ts DESC", (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_position("paper_orders", r) for r in rows]


def open_system_positions() -> list:
    """paper_trades/scalp_paper_trades/v3_paper_trades/ti_paper_trades all
    carry real capital risk regardless of ownership -- included in every
    aggregate risk view, never in a single user's view (there's no
    user_id to filter by on any of these four tables). ti_paper_trades'
    positions carry a real per-trade qty (qty_is_estimated=False); the
    other three's qty is a qty_is_estimated=True fallback of 1 -- see
    this module's own UNITS WARNING."""
    conn = _connect()
    try:
        positions = []
        for table in _ENGINE_TABLES:
            rows = conn.execute(f"SELECT * FROM {table} WHERE status='OPEN'").fetchall()
            positions.extend(_row_to_position(table, r) for r in rows)
        return positions
    finally:
        conn.close()


def all_open_positions(user_id: int | None = None) -> list:
    """user_id=None: every user's paper_orders + every system engine
    position (a full house view). user_id given: that user's paper_orders
    + every system engine position (those have no owner to filter by, so
    they're always included once a caller is asking "what capital is at
    risk right now")."""
    conn = _connect()
    try:
        if user_id is not None:
            rows = conn.execute(
                "SELECT * FROM paper_orders WHERE user_id=? AND status='OPEN' ORDER BY entry_ts DESC", (user_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM paper_orders WHERE status='OPEN' ORDER BY entry_ts DESC").fetchall()
        positions = [_row_to_position("paper_orders", r) for r in rows]
    finally:
        conn.close()
    return positions + open_system_positions()


def wallet_balance(user_id: int) -> float:
    """users.wallet_balance is ALREADY net of every open position's
    entry cost (app.py debits entry_price*qty at trade entry, credits
    back at exit) -- i.e. it's "available capital," not "total capital
    before any trade." Callers wanting total capital should add this to
    that user's current capital-at-risk (see portfolio_monitor.py)."""
    conn = _connect()
    try:
        row = conn.execute("SELECT wallet_balance FROM users WHERE id=?", (user_id,)).fetchone()
    finally:
        conn.close()
    return float(row["wallet_balance"]) if row else 0.0


def daily_realized_pnl(user_id: int | None, *, since_date: str) -> float:
    """Sum of `points` (this codebase's P&L unit throughout -- see
    agents.config's Milestone 6 section) across every position CLOSED on
    or after since_date ("YYYY-MM-DD"), across paper_orders and the three
    LEGACY engine tables only. Deliberately excludes ti_paper_trades --
    see this module's own UNITS WARNING docstring for why summing its
    already-quantity-scaled `points` in here would be wrong; use
    ti_daily_realized_pnl() for that table, and combine the two only
    after each has been converted to the same (rupee) scale -- see
    agents/risk_manager/risk_decision.py."""
    conn = _connect()
    try:
        total = 0.0
        if user_id is not None:
            row = conn.execute(
                "SELECT SUM(points) as total FROM paper_orders "
                "WHERE user_id=? AND status='CLOSED' AND exit_time >= ?",
                (user_id, since_date),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT SUM(points) as total FROM paper_orders WHERE status='CLOSED' AND exit_time >= ?",
                (since_date,),
            ).fetchone()
        total += (row["total"] or 0.0) if row else 0.0
        for table in _LEGACY_POINTS_TABLES:
            row = conn.execute(
                f"SELECT SUM(points) as total FROM {table} WHERE status='CLOSED' AND exit_time >= ?",
                (since_date,),
            ).fetchone()
            total += (row["total"] or 0.0) if row else 0.0
        return round(total, 2)
    finally:
        conn.close()


def ti_daily_realized_pnl(*, since_date: str) -> float:
    """The Trading Intelligence engine's own daily realized P&L, already
    in rupee terms (ti_paper_trades.points = (exit_price - entry_price) *
    qty, see ti_store.close_trade()) -- kept as a separate query from
    daily_realized_pnl() above rather than folded into the same SUM,
    because the two tables' `points` columns are not the same unit (see
    this module's own UNITS WARNING). Milestone 25 WS3: this is the
    query that closes the gap the M25 audit found -- the Live Portfolio
    Risk Monitor previously had no visibility into TI's daily P&L at
    all."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT SUM(points) as total FROM ti_paper_trades WHERE status='CLOSED' AND exit_time >= ?",
            (since_date,),
        ).fetchone()
        return round((row["total"] or 0.0) if row else 0.0, 2)
    finally:
        conn.close()


def closed_trade_points_today(user_id: int | None, *, since_date: str) -> list:
    """Ordered (by exit_time) `points` for every position closed on or
    after since_date -- feeds portfolio_monitor.py's running intraday
    drawdown via risk_engine.max_drawdown. Legacy tables only, same
    units reasoning as daily_realized_pnl() above."""
    conn = _connect()
    try:
        points = []
        if user_id is not None:
            rows = conn.execute(
                "SELECT points, exit_time FROM paper_orders "
                "WHERE user_id=? AND status='CLOSED' AND exit_time >= ? ORDER BY exit_time",
                (user_id, since_date),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT points, exit_time FROM paper_orders WHERE status='CLOSED' AND exit_time >= ? "
                "ORDER BY exit_time",
                (since_date,),
            ).fetchall()
        points.extend((r["points"] or 0.0, r["exit_time"]) for r in rows)
        for table in _LEGACY_POINTS_TABLES:
            rows = conn.execute(
                f"SELECT points, exit_time FROM {table} WHERE status='CLOSED' AND exit_time >= ? "
                f"ORDER BY exit_time",
                (since_date,),
            ).fetchall()
            points.extend((r["points"] or 0.0, r["exit_time"]) for r in rows)
        points.sort(key=lambda p: p[1] or "")
        return [p[0] for p in points]
    finally:
        conn.close()


def latest_greeks_for_strike(symbol: str, strike: float, direction: str) -> dict | None:
    """Reads the MOST RECENT strikes row for (symbol, strike) via a join
    to cycles -- never a live broker call. None if nothing's ever been
    logged for this symbol/strike (a brand-new symbol, or logging that
    hasn't captured this strike yet) -- callers must treat that as
    "unknown," never as zero exposure."""
    conn = _connect()
    try:
        prefix = "ce" if direction == "CE" else "pe"
        row = conn.execute(
            f"""
            SELECT s.{prefix}_delta as delta, s.{prefix}_gamma as gamma,
                   s.{prefix}_theta as theta, s.{prefix}_vega as vega, s.{prefix}_iv as iv
            FROM strikes s JOIN cycles c ON s.cycle_id = c.id
            WHERE c.symbol = ? AND s.strike = ?
            ORDER BY c.ts DESC LIMIT 1
            """,
            (symbol, strike),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row is not None else None
