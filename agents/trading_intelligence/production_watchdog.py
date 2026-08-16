"""
agents/trading_intelligence/production_watchdog.py -- Milestone 22:
the Production Watchdog. Runs every 60 seconds via the RuntimeScheduler
(config.RUNTIME_CADENCE_SECONDS["production_watchdog"]), independent of
market hours (not in scheduler.py's _MARKET_SESSION_GATED_AGENTS -- a
watchdog that stops watching when the market is closed defeats its own
purpose).

Six independent checks, each pure/read-only:
    scheduler_heartbeat      -- agents.runtime.lifecycle.get_runtime_status()
                                 is fresh and not flagged stale
    virtual_trailing_cycle   -- virtual_trailing.get_cycle_stats() is recent
    ai_live_snapshot         -- ai_live_snapshot.build_ai_live_snapshot()
                                 for a canonical watched symbol succeeds
    db_health                -- a canary write+read round-trip against
                                 this module's own table
    telegram_backlog         -- agents.intelligence_alerts.retry_tracker's
                                 due-retry count stays under a sane ceiling
    control_center_trades    -- virtual_trailing.list_states() is queryable

Each check keeps its OWN consecutive-failure counter (own table,
`watchdog_check_state` -- one row per check). This is deliberately
separate from sysadmin_store's per-AGENT failure_counter (used by
agent_runtime.run_agent_cycle() for the whole-cycle-exception case):
six independent checks inside one cycle need six independent counters,
not one. After CONSECUTIVE_FAILURES_BEFORE_ESCALATION (reuses
config.RUNTIME_MAX_CONSECUTIVE_FAILURES_BEFORE_ESCALATION, same
threshold sys_admin's own escalation already uses) consecutive failures
of the SAME check, this module escalates exactly ONCE per failure
streak (not every subsequent cycle, to avoid alert spam) via:
    1. a structured SysAdminReport (this codebase's real "structured
       error log" -- see sysadmin_report.py's own docstring; a plain
       logger.error() line is also emitted for log-tailing visibility)
    2. telegram_notifier.send_admin_alert()
    3. runtime_events.emit_safe() (AGENT_ESCALATED, same event type
       agent_runtime.py's own _escalate() uses)
The escalated flag resets the moment the check passes again.

Read-only / paper-trade-only: every check here only READS
already-computed state. Nothing here ever places, modifies, or cancels
a broker order -- and nothing in this package's own __init__.py
safety rule (verified by test_safety.py's AST scan, which covers every
file in this package automatically) would allow it to.
"""
import dataclasses
import datetime as dt
import logging
import os
import sqlite3
import time

from .. import config, timekeeping
from ..runtime import runtime_events
from ..sys_admin import sysadmin_report, sysadmin_store
from . import ai_live_snapshot, telegram_notifier, virtual_trailing

log = logging.getLogger(__name__)

DB_PATH = "oi_history.db"

CHECK_NAMES = (
    "scheduler_heartbeat",
    "virtual_trailing_cycle",
    "ai_live_snapshot",
    "db_health",
    "telegram_backlog",
    "control_center_trades",
)

# Reuses the SAME threshold sys_admin's own agent-level escalation
# already uses (default 3) -- this spec's "3 consecutive failures"
# requirement is exactly that constant's existing default, not a new
# number invented for this module.
CONSECUTIVE_FAILURES_BEFORE_ESCALATION = config.RUNTIME_MAX_CONSECUTIVE_FAILURES_BEFORE_ESCALATION

# Chosen, not calibrated -- generous relative to the underlying
# cadences (trading_intelligence's own 180s default cycle, this
# watchdog's own 60s cycle) so ordinary cycle-to-cycle jitter never
# false-alarms.
VIRTUAL_TRAILING_STALE_SECONDS = 900
SCHEDULER_STALE_SECONDS = 900
TELEGRAM_BACKLOG_MAX = int(os.getenv("WATCHDOG_TELEGRAM_BACKLOG_MAX", "20"))

# A symbol guaranteed present in TI_WATCHED_SYMBOLS's own default list
# (agents/config.py) -- used to exercise ai_live_snapshot.py's real
# code path every cycle without needing to pick a "current" symbol
# (this module has no UI/session context to know one).
CANONICAL_SNAPSHOT_SYMBOL = "NIFTY"

CYCLE_LOG_RETENTION_ROWS = 500   # rolling-window trim, not unbounded growth


@dataclasses.dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    latency_ms: float | None = None


def _connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db() -> None:
    conn = _connect()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watchdog_check_state (
                check_name            TEXT PRIMARY KEY,
                consecutive_failures  INTEGER NOT NULL DEFAULT 0,
                last_ok               INTEGER NOT NULL DEFAULT 1,
                last_ts               TEXT,
                last_detail           TEXT,
                escalated             INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watchdog_cycle_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT NOT NULL,
                duration_ms  REAL NOT NULL,
                overall_ok   INTEGER NOT NULL,
                checks_json  TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_watchdog_cycle_log_ts ON watchdog_cycle_log(ts)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS watchdog_canary (
                id    INTEGER PRIMARY KEY CHECK (id = 1),
                value TEXT
            )
        """)
        conn.execute("INSERT OR IGNORE INTO watchdog_canary (id, value) VALUES (1, 'init')")
        conn.commit()
    finally:
        conn.close()


# --------------------------------------------------------------------
# Individual checks -- each returns a CheckResult, never raises (a
# check that itself throws is caught by _run_check() below and
# recorded as a failure with the exception text as its detail, exactly
# like every other honest-degrade convention in this package).
# --------------------------------------------------------------------

def _check_scheduler_heartbeat() -> CheckResult:
    # Local import: agents.runtime.lifecycle imports agent_runtime,
    # and agent_runtime registers this module's own cycle function --
    # a module-level import here would be a circular import. Same
    # lazy-import pattern agent_runtime.py's own _dev_agent_cycle()
    # already uses for the identical reason.
    from ..runtime import lifecycle

    status = lifecycle.get_runtime_status()
    if status.get("watchdog_stale"):
        return CheckResult("scheduler_heartbeat", False, "runtime scheduler's own watchdog reports stale cycles")
    last_ts = status.get("last_cycle_timestamp")
    if last_ts is None:
        return CheckResult("scheduler_heartbeat", False, "scheduler has never completed a cycle")
    age = (dt.datetime.now() - dt.datetime.fromisoformat(last_ts)).total_seconds()
    if age > SCHEDULER_STALE_SECONDS:
        return CheckResult("scheduler_heartbeat", False, f"last scheduler cycle was {round(age)}s ago")
    return CheckResult("scheduler_heartbeat", True, f"last cycle {round(age)}s ago")


def _check_virtual_trailing_cycle() -> CheckResult:
    if not config.TI_ENABLE_VIRTUAL_TRAILING:
        return CheckResult("virtual_trailing_cycle", True, "skipped -- TI_ENABLE_VIRTUAL_TRAILING is off")
    stats = virtual_trailing.get_cycle_stats()
    last_ts = stats.get("last_cycle_ts")
    if last_ts is None:
        return CheckResult("virtual_trailing_cycle", False, "engine has never completed a cycle")
    # Milestone 25: now_ist(), not dt.datetime.now() -- last_ts comes from
    # virtual_trailing.record_cycle_duration()'s own _now(), which is
    # timekeeping.now_ist_iso() as of this milestone; a mismatched clock
    # here would silently misreport staleness.
    age = (timekeeping.now_ist() - dt.datetime.fromisoformat(last_ts)).total_seconds()
    if age > VIRTUAL_TRAILING_STALE_SECONDS:
        return CheckResult("virtual_trailing_cycle", False, f"last cycle was {round(age)}s ago")
    return CheckResult("virtual_trailing_cycle", True, f"last cycle {round(age)}s ago, "
                                                         f"took {stats.get('last_cycle_duration_ms')}ms")


def _check_ai_live_snapshot() -> CheckResult:
    started = time.monotonic()
    try:
        snapshot = ai_live_snapshot.build_ai_live_snapshot(CANONICAL_SNAPSHOT_SYMBOL)
    except Exception as e:
        latency_ms = round((time.monotonic() - started) * 1000, 2)
        return CheckResult("ai_live_snapshot", False, f"raised: {e}", latency_ms)
    latency_ms = round((time.monotonic() - started) * 1000, 2)
    if snapshot is None:
        return CheckResult("ai_live_snapshot", False, "no cycle data available yet", latency_ms)
    return CheckResult("ai_live_snapshot", True, f"{CANONICAL_SNAPSHOT_SYMBOL} snapshot built", latency_ms)


def _check_db_health() -> CheckResult:
    started = time.monotonic()
    try:
        conn = _connect()
        try:
            marker = dt.datetime.now().isoformat()
            conn.execute("UPDATE watchdog_canary SET value=? WHERE id=1", (marker,))
            conn.commit()
            row = conn.execute("SELECT value FROM watchdog_canary WHERE id=1").fetchone()
        finally:
            conn.close()
    except Exception as e:
        latency_ms = round((time.monotonic() - started) * 1000, 2)
        return CheckResult("db_health", False, f"raised: {e}", latency_ms)
    latency_ms = round((time.monotonic() - started) * 1000, 2)
    if row is None or row["value"] != marker:
        return CheckResult("db_health", False, "write/read round-trip mismatch", latency_ms)
    return CheckResult("db_health", True, "write/read round-trip OK", latency_ms)


def _check_telegram_backlog() -> CheckResult:
    from ..intelligence_alerts import retry_tracker

    try:
        backlog = len(retry_tracker.get_due_retries())
    except Exception as e:
        return CheckResult("telegram_backlog", False, f"raised: {e}")
    if backlog > TELEGRAM_BACKLOG_MAX:
        return CheckResult("telegram_backlog", False, f"{backlog} due retries (> {TELEGRAM_BACKLOG_MAX})")
    return CheckResult("telegram_backlog", True, f"{backlog} due retries")


def _check_control_center_trades() -> CheckResult:
    try:
        count = len(virtual_trailing.list_states())
    except Exception as e:
        return CheckResult("control_center_trades", False, f"raised: {e}")
    return CheckResult("control_center_trades", True, f"{count} tracked trades")


_CHECK_FUNCS = {
    "scheduler_heartbeat": _check_scheduler_heartbeat,
    "virtual_trailing_cycle": _check_virtual_trailing_cycle,
    "ai_live_snapshot": _check_ai_live_snapshot,
    "db_health": _check_db_health,
    "telegram_backlog": _check_telegram_backlog,
    "control_center_trades": _check_control_center_trades,
}


def _run_check(name: str) -> CheckResult:
    try:
        return _CHECK_FUNCS[name]()
    except Exception as e:
        return CheckResult(name, False, f"check itself raised: {e}")


# --------------------------------------------------------------------
# Per-check state + escalation
# --------------------------------------------------------------------

def _get_check_state(conn, name: str) -> dict | None:
    row = conn.execute("SELECT * FROM watchdog_check_state WHERE check_name=?", (name,)).fetchone()
    return dict(row) if row else None


def _escalate(result: CheckResult, *, consecutive_failures: int) -> None:
    reason = (f"production_watchdog check {result.name!r} failed {consecutive_failures} consecutive cycles "
              f"(>= {CONSECUTIVE_FAILURES_BEFORE_ESCALATION}): {result.detail}")
    report = sysadmin_report.build(
        module="production_watchdog", action="escalate", reason=reason, confidence=90,
        evidence={"check": result.name, "consecutive_failures": consecutive_failures, "detail": result.detail},
        affected_components=[result.name], severity="critical",
    )
    sysadmin_store.record_report(report)
    log.error(f"[PRODUCTION_WATCHDOG] {reason}")
    telegram_notifier.send_admin_alert(f"\U0001F6A8 <b>Production Watchdog</b>\n{reason}")
    runtime_events.emit_safe("production_watchdog", runtime_events.AGENT_ESCALATED, {
        "check": result.name, "consecutive_failures": consecutive_failures, "detail": result.detail,
    })


def _update_check_state(conn, result: CheckResult, *, now: str) -> int:
    """Updates this check's row, returns the NEW consecutive_failures
    count. Escalates (exactly once per failure streak) when that count
    crosses CONSECUTIVE_FAILURES_BEFORE_ESCALATION."""
    prior = _get_check_state(conn, result.name)
    prior_failures = prior["consecutive_failures"] if prior else 0
    prior_escalated = bool(prior["escalated"]) if prior else False

    if result.ok:
        consecutive_failures, escalated = 0, False
    else:
        consecutive_failures = prior_failures + 1
        escalated = prior_escalated

    conn.execute(
        "INSERT INTO watchdog_check_state (check_name, consecutive_failures, last_ok, last_ts, last_detail, escalated) "
        "VALUES (?,?,?,?,?,?) "
        "ON CONFLICT(check_name) DO UPDATE SET consecutive_failures=excluded.consecutive_failures, "
        "last_ok=excluded.last_ok, last_ts=excluded.last_ts, last_detail=excluded.last_detail, "
        "escalated=excluded.escalated",
        (result.name, consecutive_failures, int(result.ok), now, result.detail, int(escalated)),
    )

    if not result.ok and consecutive_failures >= CONSECUTIVE_FAILURES_BEFORE_ESCALATION and not escalated:
        conn.execute("UPDATE watchdog_check_state SET escalated=1 WHERE check_name=?", (result.name,))
        conn.commit()
        _escalate(result, consecutive_failures=consecutive_failures)
        return consecutive_failures

    return consecutive_failures


# --------------------------------------------------------------------
# Public entrypoints
# --------------------------------------------------------------------

def run_watchdog_cycle() -> dict:
    """The full 60s cycle: runs all six checks, updates each check's own
    consecutive-failure counter (escalating as needed), logs one rolling
    watchdog_cycle_log row, and returns a summary dict. Never raises."""
    import json

    started = time.monotonic()
    now = timekeeping.now_ist_iso()
    results = [_run_check(name) for name in CHECK_NAMES]

    conn = _connect()
    try:
        state_by_check = {}
        for result in results:
            consecutive_failures = _update_check_state(conn, result, now=now)
            state_by_check[result.name] = {
                "ok": result.ok, "detail": result.detail, "latency_ms": result.latency_ms,
                "consecutive_failures": consecutive_failures,
            }
        conn.commit()

        duration_ms = round((time.monotonic() - started) * 1000, 2)
        overall_ok = all(r.ok for r in results)
        conn.execute(
            "INSERT INTO watchdog_cycle_log (ts, duration_ms, overall_ok, checks_json) VALUES (?,?,?,?)",
            (now, duration_ms, int(overall_ok), json.dumps(state_by_check)),
        )
        # Rolling-window trim -- keep only the most recent CYCLE_LOG_RETENTION_ROWS rows.
        conn.execute(
            "DELETE FROM watchdog_cycle_log WHERE id NOT IN "
            "(SELECT id FROM watchdog_cycle_log ORDER BY id DESC LIMIT ?)",
            (CYCLE_LOG_RETENTION_ROWS,),
        )
        conn.commit()
    finally:
        conn.close()

    return {"ts": now, "duration_ms": duration_ms, "overall_ok": overall_ok, "checks": state_by_check}


def get_metrics() -> dict:
    """Rolling metrics computed from watchdog_cycle_log's own retained
    window: avg/max cycle time, last successful cycle, trailing exits
    in the last 1h, and the most recent AI-snapshot-check latency."""
    import json

    conn = _connect()
    try:
        rows = conn.execute("SELECT ts, duration_ms, overall_ok, checks_json FROM watchdog_cycle_log "
                             "ORDER BY id DESC LIMIT ?", (CYCLE_LOG_RETENTION_ROWS,)).fetchall()
    finally:
        conn.close()

    if not rows:
        avg_cycle_ms = max_cycle_ms = last_successful_cycle = snapshot_refresh_latency_ms = None
    else:
        durations = [r["duration_ms"] for r in rows]
        avg_cycle_ms = round(sum(durations) / len(durations), 2)
        max_cycle_ms = max(durations)
        last_successful_cycle = next((r["ts"] for r in rows if r["overall_ok"]), None)
        snapshot_refresh_latency_ms = None
        for r in rows:
            latency = json.loads(r["checks_json"]).get("ai_live_snapshot", {}).get("latency_ms")
            if latency is not None:
                snapshot_refresh_latency_ms = latency
                break

    # Milestone 25: now_ist(), not dt.datetime.now() -- compared below
    # against virtual_trailing_state.updated_ts, which virtual_trailing.py
    # now stamps via timekeeping.now_ist_iso().
    since = (timekeeping.now_ist() - dt.timedelta(hours=1)).isoformat()
    trailing_exits_last_1h = sum(
        1 for row in virtual_trailing.list_states()
        if row["state"] == "EXITED" and row["updated_ts"] > since
    )

    return {
        "avg_cycle_ms": avg_cycle_ms,
        "max_cycle_ms": max_cycle_ms,
        "last_successful_cycle": last_successful_cycle,
        "trailing_exits_last_1h": trailing_exits_last_1h,
        "snapshot_refresh_latency_ms": snapshot_refresh_latency_ms,
    }


_WIDGET_LABELS = {
    "scheduler_heartbeat": ("Scheduler", "RUNNING", "DOWN"),
    "ai_live_snapshot": ("Snapshot", "LIVE", "STALE"),
    "virtual_trailing_cycle": ("Trailing Engine", "ACTIVE", "STALE"),
    "db_health": ("DB", "HEALTHY", "DEGRADED"),
    "telegram_backlog": ("Telegram", "CONNECTED", "BACKLOGGED"),
}


def get_status() -> dict:
    """The full payload GET /api/monitoring/health exposes: current
    per-check state, rolling metrics, and the 5-label dashboard-widget
    summary (Scheduler/Snapshot/Trailing Engine/DB/Telegram)."""
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM watchdog_check_state").fetchall()
    finally:
        conn.close()
    checks = {r["check_name"]: dict(r) for r in rows}
    for row in checks.values():
        row["last_ok"] = bool(row["last_ok"])
        row["escalated"] = bool(row["escalated"])

    widget = {}
    for check_name, (label, ok_label, bad_label) in _WIDGET_LABELS.items():
        row = checks.get(check_name)
        healthy = row is None or row["last_ok"]   # no data yet -> not-yet-failing, not a false alarm
        widget[label] = ok_label if healthy else bad_label

    return {"checks": checks, "metrics": get_metrics(), "widget": widget}
