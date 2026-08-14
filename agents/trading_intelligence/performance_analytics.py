"""
agents/trading_intelligence/performance_analytics.py -- Milestone 23:
Performance Analytics & Strategy Intelligence.

Pure read + arithmetic over data this repository already stores -- no
new table, no new broker call, no new scheduled write. Every function
here is read-only and paper-trade-only, the same safety rule this
package's __init__.py already establishes (verified for every module in
this package, this one included, by test_agents/trading_intelligence/
test_safety.py's AST scan).

Five surfaces:
1. Summary stats (Win Rate/Expectancy/Profit Factor/Max Drawdown/Sharpe/
   Sortino/Recovery Factor/Equity Curve) -- a thin re-export of
   paper_trading.performance_stats(), already built in Milestone 11
   (Module 11.6) on agents.quant_researcher.metrics.compute_stats(). Not
   reimplemented here.
2. Hourly/weekday performance heatmaps -- NEW: buckets this engine's own
   ti_paper_trades CLOSED trades by entry hour-of-day (0-23) and weekday
   (Monday-Sunday), each bucket run through the SAME compute_stats()
   every other performance surface in this repo uses, so a heatmap
   cell's "win rate" means the exact same thing the top-line "win rate"
   does.
3. Strategy-wise comparison -- NEW: this repository runs four
   independent paper-trading engines, each with its own dedicated table
   (see ti_store.py's own module docstring for why one engine never
   shares another's table): the Swing/SR Engine (app.py's own
   `paper_trades`), the Scalp Engine (`scalp_paper_trades`), SR Engine
   V3 (`v3_paper_trades`), and this package's own Trading Intelligence
   engine (`ti_paper_trades`). Read-only SELECT against each (the same
   cross-table read pattern data_access.py already established for
   oi_history/market_structure -- this package already reads tables it
   doesn't own, just never before wrote across all four paper-trading
   ones at once), fed through the same compute_stats() -- an
   apples-to-apples comparison, not four different engines' own bespoke
   stats math compared informally.
4. Trailing efficiency -- NEW: for every EXITED virtual_trailing_state
   row, what fraction of the peak premium move (highest_premium -
   entry_price) the Virtual Trailing Engine actually captured before
   giving it back, and how that virtual exit compares to the real
   trade's own fixed-target/SL outcome. Purely observational, exactly
   like every other virtual_trailing_state read in this package --
   never affects, and never even reads back into, a real trade.
5. AI confidence vs. actual outcome -- a thin re-export of
   ai_trading_engine.calibration_report(), built in Milestone 10/11 and
   never before surfaced on a dashboard. Not reimplemented here.

Every bucketed stat here uses the SAME "insufficient history reports a
sample size and an honest note, never a fabricated percentage"
discipline ai_trading_engine.calibration_report()/
paper_trade_diagnostics.py already established -- MIN_SAMPLE below
matches ai_trading_engine.CALIBRATION_MIN_SAMPLE.
"""
import datetime as dt
import sqlite3

from . import ai_trading_engine, paper_trading, ti_store, virtual_trailing
from ..quant_researcher import metrics

DB_PATH = "oi_history.db"
MIN_SAMPLE = 5   # matches ai_trading_engine.CALIBRATION_MIN_SAMPLE

_WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# Milestone 23 audit fix (Medium finding #1): get_hourly_heatmap()/
# get_weekday_heatmap() bucket off entry_time, a naive
# dt.datetime.now().isoformat() string written by ti_store._now()/
# virtual_trailing._now() -- there is no timezone recorded in that string,
# so it's only IST wall-clock time because the WRITING server's OS
# timezone happens to be configured as Asia/Kolkata (matching app.py's own
# now_ist()/IST_OFFSET convention in spirit, but not enforced the same
# explicit way). A naive string with no recorded origin can't be safely
# re-converted after the fact -- guessing would just trade one silent
# assumption for another, possibly wrong, one. So this checks the READING
# server's own current UTC offset instead, and surfaces an honest warning
# when it doesn't match IST, rather than silently mislabeling every
# bucket the way the pre-fix version did.
_IST_OFFSET = dt.timedelta(hours=5, minutes=30)


def _server_timezone_matches_ist() -> bool:
    return dt.datetime.now().astimezone().utcoffset() == _IST_OFFSET


def _timezone_note() -> str | None:
    if _server_timezone_matches_ist():
        return None
    return (
        "WARNING: this server's own timezone is not IST (+05:30) -- the buckets "
        "below assume entry_time is already IST wall-clock time (see ti_store.py/"
        "virtual_trailing.py's own _now()) and will be mislabeled if that's not "
        "actually the case on the server that WROTE these trades"
    )

# The four independently-tabled paper-trading engines this repository
# runs -- see module docstring point 3. Every table shares the same
# minimum shape (symbol, points, exit_reason, status) that
# metrics.compute_stats() needs; nothing else here assumes any other
# column exists.
_STRATEGY_TABLES = (
    ("SWING", "paper_trades"),
    ("SCALP", "scalp_paper_trades"),
    ("V3", "v3_paper_trades"),
    ("TRADING_INTELLIGENCE", "ti_paper_trades"),
)


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def get_summary(*, symbol: str | None = None) -> dict:
    """Win Rate/Profit Factor/Max Drawdown/Expectancy/Sharpe/Sortino/
    Recovery Factor/Equity Curve -- unchanged re-export of
    paper_trading.performance_stats(), so this dashboard and the
    existing "Paper Trading Performance" panel never disagree."""
    return paper_trading.performance_stats(symbol=symbol)


def _bucket_stats(trades_by_key: dict) -> dict:
    out = {}
    for key, trades in trades_by_key.items():
        stats = metrics.compute_stats(trades)
        sufficient = stats["total_trades"] >= MIN_SAMPLE
        out[key] = {
            **stats,
            "sufficient_sample": sufficient,
            "note": None if sufficient else (
                f"only {stats['total_trades']} closed trade(s) -- need >= {MIN_SAMPLE} for a reliable read"
            ),
        }
    return out


def get_hourly_heatmap(*, symbol: str | None = None) -> dict:
    """One bucket per entry hour-of-day (0-23, the same naive
    datetime.now().isoformat() every ti_store timestamp already uses),
    each run through compute_stats(). Only hours with at least one
    CLOSED trade appear -- an hour with zero trades is omitted, never
    reported as a fabricated 0% win rate. `timezone_note` (Milestone 23
    audit fix): non-None when THIS server's own clock isn't IST -- see
    _timezone_note()'s own docstring for why this is a warning, not a
    conversion."""
    trades = ti_store.list_closed_trades(symbol=symbol, limit=10_000)
    by_hour: dict = {}
    for t in trades:
        try:
            hour = dt.datetime.fromisoformat(t["entry_time"]).hour
        except (TypeError, ValueError):
            continue
        by_hour.setdefault(hour, []).append(t)
    return {
        "by_hour": _bucket_stats({str(h): ts for h, ts in sorted(by_hour.items())}),
        "min_sample": MIN_SAMPLE,
        "timezone_note": _timezone_note(),
    }


def get_weekday_heatmap(*, symbol: str | None = None) -> dict:
    """Same bucketing as get_hourly_heatmap(), by weekday name
    (Monday-Sunday, calendar order) instead of hour."""
    trades = ti_store.list_closed_trades(symbol=symbol, limit=10_000)
    by_weekday: dict = {}
    for t in trades:
        try:
            weekday = dt.datetime.fromisoformat(t["entry_time"]).weekday()
        except (TypeError, ValueError):
            continue
        by_weekday.setdefault(_WEEKDAY_NAMES[weekday], []).append(t)
    ordered = {name: by_weekday[name] for name in _WEEKDAY_NAMES if name in by_weekday}
    return {"by_weekday": _bucket_stats(ordered), "min_sample": MIN_SAMPLE, "timezone_note": _timezone_note()}


def _read_strategy_trades(table: str, *, symbol: str | None) -> list | None:
    """Read-only SELECT of one engine's own CLOSED trades. Returns None
    (not []) when the table itself doesn't exist yet -- e.g. a fresh
    test DB that never ran app.py's own init_db() -- so callers can tell
    "this engine has genuinely made zero trades" apart from "this
    engine's table was never created". Table name is never
    user-supplied -- always one of the four literal names in
    _STRATEGY_TABLES above -- so the % string-format below is safe."""
    conn = _connect()
    try:
        query = "SELECT points, exit_reason FROM %s WHERE status='CLOSED'" % table
        params: list = []
        if symbol:
            query += " AND symbol=?"
            params.append(symbol)
        rows = conn.execute(query, params).fetchall()
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()
    return [dict(r) for r in rows]


def get_strategy_comparison(*, symbol: str | None = None) -> dict:
    """Head-to-head compute_stats() for every paper-trading engine this
    repository runs -- see module docstring point 3. A strategy whose
    table doesn't exist yet reports available=False, never a fabricated
    zero-trade stat block.

    Milestone 24 fix (M23 audit, Low finding): an insufficient-sample
    strategy now carries the same explanatory `note` every other surface
    in this module already gives (_bucket_stats()'s own `note` logic,
    ai_trading_engine.calibration_report()'s `note`) -- previously this
    was the one surface that reported a real win_rate/profit_factor next
    to sufficient_sample=False with no explanation at all."""
    out = {}
    for name, table in _STRATEGY_TABLES:
        trades = _read_strategy_trades(table, symbol=symbol)
        if trades is None:
            out[name] = {"available": False, "reason": f"{table} does not exist yet"}
            continue
        stats = metrics.compute_stats(trades)
        sufficient = stats["total_trades"] >= MIN_SAMPLE
        out[name] = {
            "available": True,
            **stats,
            "sufficient_sample": sufficient,
            "note": None if sufficient else (
                f"only {stats['total_trades']} closed trade(s) -- need >= {MIN_SAMPLE} for a reliable read"
            ),
        }
    return out


def get_trailing_efficiency(*, symbol: str | None = None) -> dict:
    """How much of the Virtual Trailing Engine's own tracked peak move
    (highest_premium - entry_price) was actually captured by the time a
    shadow position virtually exited, and how that compares to the real
    trade's own fixed-target/SL outcome. Purely observational -- see
    virtual_trailing.py's own module docstring: this engine never
    touches the real trade.

    Only EXITED states are scored (an ACTIVE state's final capture isn't
    known yet); `stage_distribution` below covers every state (ACTIVE +
    EXITED, via virtual_trailing.stage()) so the dashboard can also show
    how many trades are CURRENTLY in RUNNER/TRAILING/COST_PROTECTED/
    OPEN right now.

    Milestone 23 audit fix (Medium finding #2): a state only enters the
    capture-efficiency average once its peak_gain has crossed
    virtual_trailing.BREAKEVEN_TRIGGER_POINTS -- below that, peak_gain is
    noise-level, and virtual_points / peak_gain produces a mathematically
    correct but practically meaningless outlier (a 0.5pt peak followed by
    a -20pt stop-out used to report as "-4000% captured"). Matches this
    module's own MIN_SAMPLE "don't report on a statistically meaningless
    slice" discipline -- excluded here, never clamped to a fake number."""
    states = virtual_trailing.list_states(symbol=symbol)
    stage_distribution: dict = {}
    for s in states:
        stage_distribution[s["stage"]] = stage_distribution.get(s["stage"], 0) + 1

    exited = [s for s in states if s["state"] == "EXITED"]
    real_trades_by_id = {t["id"]: t for t in ti_store.list_closed_trades(symbol=symbol, limit=10_000)}

    capture_pcts: list = []
    uplifts: list = []
    for s in exited:
        peak_gain = round(s["highest_premium"] - s["entry_price"], 2)
        if peak_gain < virtual_trailing.BREAKEVEN_TRIGGER_POINTS or s["exit_price"] is None:
            continue
        virtual_points = round(s["exit_price"] - s["entry_price"], 2)
        capture_pcts.append(round(virtual_points / peak_gain * 100, 1))
        real_trade = real_trades_by_id.get(s["trade_id"])
        if real_trade is not None and real_trade.get("points") is not None:
            uplifts.append(round(virtual_points - real_trade["points"], 2))

    sufficient = len(capture_pcts) >= MIN_SAMPLE
    return {
        "stage_distribution": stage_distribution,
        "exited_sample_size": len(exited),
        "scored_sample_size": len(capture_pcts),
        "uplift_sample_size": len(uplifts),
        "sufficient_sample": sufficient,
        "avg_capture_efficiency_pct": round(sum(capture_pcts) / len(capture_pcts), 1) if sufficient else None,
        "avg_trailing_uplift_points": round(sum(uplifts) / len(uplifts), 2) if sufficient and uplifts else None,
        "note": None if sufficient else (
            f"only {len(capture_pcts)} scoreable exited trade(s) -- need >= {MIN_SAMPLE} for a reliable read"
        ),
        "min_sample": MIN_SAMPLE,
    }


def get_confidence_calibration(*, dimension: str | None = None):
    """AI confidence vs. actual outcome -- unchanged re-export of
    ai_trading_engine.calibration_report(), built in Milestone 10/11 and
    never before surfaced on a dashboard. `dimension` (optional): one of
    ai_trading_engine.CALIBRATION_DIMENSIONS ("regime"/
    "timeframe_alignment"/"quality_tier") -- see that function's own
    docstring for the exact return-shape change this triggers."""
    return ai_trading_engine.calibration_report(dimension=dimension)


def get_full_report(*, symbol: str | None = None, calibration_dimension: str | None = None) -> dict:
    """Everything this module computes, in one call -- the payload GET
    /api/performance-analytics/report returns. Five independent reads,
    each already cheap (no broker call, no live fetch, pure SQLite
    SELECTs against already-written tables) -- see each function's own
    docstring."""
    return {
        "summary": get_summary(symbol=symbol),
        "hourly_heatmap": get_hourly_heatmap(symbol=symbol),
        "weekday_heatmap": get_weekday_heatmap(symbol=symbol),
        "strategy_comparison": get_strategy_comparison(symbol=symbol),
        "trailing_efficiency": get_trailing_efficiency(symbol=symbol),
        "confidence_calibration": get_confidence_calibration(dimension=calibration_dimension),
    }
