"""
agents/risk_manager/data_access.py -- the ONLY module in
agents/risk_manager/ that reads live (paper-trading) position/wallet/
Greeks data. Every query here is a plain SELECT against tables app.py
already owns and writes (paper_orders, paper_trades, scalp_paper_trades,
v3_paper_trades, users, cycles, strikes) -- never a live broker API
call. In particular this module NEVER touches the Angel One session
object (no .get_option_greeks(), no live login) -- a documented landmine
in this repo (hitting /live-positions in a test already triggered a
real duplicate broker login once, per this project's own history).
Theta/Vega, when available at all, come from whatever the live app
already wrote into the `strikes` table the last time it polled the
broker -- read-only here, never triggered fresh.

Schema source: paper_orders/paper_trades/scalp_paper_trades/
v3_paper_trades/users/wallet_transactions/cycles/strikes are all defined
in app.py's own init_db(); this module never creates or migrates them
(that stays app.py's responsibility) -- only reads.
"""
import dataclasses
import sqlite3

DB_PATH = "oi_history.db"

# System-wide (no user_id) paper-trading tables -- each independent
# engine's own open positions, not attributable to a specific user.
_ENGINE_TABLES = ("paper_trades", "scalp_paper_trades", "v3_paper_trades")


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@dataclasses.dataclass
class Position:
    source: str  # "paper_orders" | "paper_trades" | "scalp_paper_trades" | "v3_paper_trades"
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


def _row_to_position(source: str, row: sqlite3.Row) -> Position:
    d = dict(row)
    return Position(
        source=source, id=d.get("id"), user_id=d.get("user_id"), symbol=d.get("symbol"),
        strike=d.get("strike"), direction=d.get("direction"), entry_price=float(d.get("entry_price") or 0.0),
        sl_price=(float(d["sl_price"]) if d.get("sl_price") is not None else None),
        target_price=(float(d["target_price"]) if d.get("target_price") is not None else None),
        qty=int(d.get("qty") or 1), entry_time=d.get("entry_time"),
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
    """paper_trades/scalp_paper_trades/v3_paper_trades carry real capital
    risk regardless of ownership -- included in every aggregate risk
    view, never in a single user's view (there's no user_id to filter by
    on these three tables)."""
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
    or after since_date ("YYYY-MM-DD"), across all four tables."""
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
        for table in _ENGINE_TABLES:
            row = conn.execute(
                f"SELECT SUM(points) as total FROM {table} WHERE status='CLOSED' AND exit_time >= ?",
                (since_date,),
            ).fetchone()
            total += (row["total"] or 0.0) if row else 0.0
        return round(total, 2)
    finally:
        conn.close()


def closed_trade_points_today(user_id: int | None, *, since_date: str) -> list:
    """Ordered (by exit_time) `points` for every position closed on or
    after since_date -- feeds portfolio_monitor.py's running intraday
    drawdown via risk_engine.max_drawdown."""
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
        for table in _ENGINE_TABLES:
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
