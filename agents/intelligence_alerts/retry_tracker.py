"""
agents/intelligence_alerts/retry_tracker.py -- Milestone 15, Phase 2:
Delivery Retry Protection. SQLite-backed, own isolated table
(intelligence_alert_retry_state) -- one row per delivery IDENTITY (the
same fingerprint string cooldown.make_fingerprint() already produces,
by convention -- this module doesn't require that shape, just a stable
string), tracking attempt count, the last failed send's own message
text (so a later retry can resend the SAME content), and the timestamp
of that failure. A successful delivery clears the row via
record_success() -- there is nothing left to retry.

Fixed exponential backoff schedule
(agents.config.INTELLIGENCE_ALERT_RETRY_BACKOFF_SECONDS: 30/60/120/300
by default) -- record_failure() advances the attempt count and returns
whether this identity is now exhausted
(agents.config.INTELLIGENCE_ALERT_RETRY_MAX_ATTEMPTS). is_due() /
get_due_retries() check whether enough time has passed since the last
failure for the CURRENT attempt count's own backoff delay. An exhausted
identity is left in the table as history (not deleted) but never
reported as due again until record_success() clears it.

Deliberately does NOT touch agents/intelligence_alerts/dedup_store.py's
own suppression state -- retry is about actually delivering a decision
that was ALREADY made (dedup_store.should_suppress() already said "yes,
alert" once), not about re-deciding whether to alert at all. Keeping
these two concerns separate avoids re-opening Phase 1's already-shipped
dedup contract.
"""
import datetime as dt
import logging
import sqlite3

from agents import config as agents_config
from agents.ops import event_log as ops_event_log, models as ops_models

DB_PATH = "oi_history.db"
log = logging.getLogger("intelligence_alerts.retry")


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
            CREATE TABLE IF NOT EXISTS intelligence_alert_retry_state (
                identity        TEXT PRIMARY KEY,
                message         TEXT NOT NULL,
                attempt_count   INTEGER NOT NULL DEFAULT 0,
                last_failure_at TEXT NOT NULL,
                exhausted       INTEGER NOT NULL DEFAULT 0
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def backoff_seconds(attempt_count: int) -> int:
    """Delay before the NEXT attempt, given this many recorded failures
    so far (1-indexed: after the 1st failure, this is the delay before
    the 2nd attempt). Caps at the schedule's last value for any attempt
    count beyond its length -- never grows unbounded."""
    schedule = agents_config.INTELLIGENCE_ALERT_RETRY_BACKOFF_SECONDS
    index = min(max(attempt_count, 1) - 1, len(schedule) - 1)
    return schedule[index]


def _get(identity: str):
    conn = _connect()
    try:
        return conn.execute(
            "SELECT * FROM intelligence_alert_retry_state WHERE identity = ?", (identity,)
        ).fetchone()
    finally:
        conn.close()


def _is_row_due(row, now: dt.datetime) -> bool:
    if row["exhausted"]:
        return False
    elapsed = (now - dt.datetime.fromisoformat(row["last_failure_at"])).total_seconds()
    return elapsed >= backoff_seconds(row["attempt_count"])


def record_failure(identity: str, message: str, now=None) -> dict:
    """Advances this identity's attempt count by one and logs either
    DELIVERY_RETRY_SCHEDULED (with the backoff before the next attempt)
    or DELIVERY_RETRY_EXHAUSTED (once max attempts is reached). Returns
    the new state as a dict: {"identity", "attempt_count", "exhausted"}."""
    now = now or dt.datetime.now()
    row = _get(identity)
    attempt_count = (row["attempt_count"] if row else 0) + 1
    max_attempts = agents_config.INTELLIGENCE_ALERT_RETRY_MAX_ATTEMPTS
    exhausted = attempt_count >= max_attempts

    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO intelligence_alert_retry_state "
            "(identity, message, attempt_count, last_failure_at, exhausted) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(identity) DO UPDATE SET "
            "message=excluded.message, attempt_count=excluded.attempt_count, "
            "last_failure_at=excluded.last_failure_at, exhausted=excluded.exhausted",
            (identity, message, attempt_count, now.isoformat(), int(exhausted)),
        )
        conn.commit()
    finally:
        conn.close()

    if exhausted:
        log.info(f"DELIVERY_RETRY_EXHAUSTED identity={identity!r} attempt_count={attempt_count}")
        ops_event_log.record_event_safe(
            ops_models.RETRY_EXHAUSTED, {"identity": identity, "attempt_count": attempt_count}, now=now,
        )
    else:
        delay = backoff_seconds(attempt_count)
        log.info(
            f"DELIVERY_RETRY_SCHEDULED identity={identity!r} attempt_count={attempt_count} backoff_seconds={delay}"
        )
        ops_event_log.record_event_safe(
            ops_models.RETRY_SCHEDULED,
            {"identity": identity, "attempt_count": attempt_count, "backoff_seconds": delay}, now=now,
        )
    return {"identity": identity, "attempt_count": attempt_count, "exhausted": exhausted}


def record_success(identity: str) -> None:
    """Clears retry state entirely for this identity -- nothing left to
    retry. A no-op (not an error) if the identity never failed."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM intelligence_alert_retry_state WHERE identity = ?", (identity,))
        conn.commit()
    finally:
        conn.close()


def is_due(identity: str, now=None) -> bool:
    """True if this identity has a pending failure whose backoff has
    elapsed and hasn't been exhausted. False for an identity with no
    recorded failure at all (nothing to retry) or one already
    exhausted."""
    now = now or dt.datetime.now()
    row = _get(identity)
    if row is None:
        return False
    return _is_row_due(row, now)


def get_due_retries(now=None) -> list:
    """Every identity across the whole table whose backoff has elapsed
    and isn't exhausted, as {"identity", "message", "attempt_count"}
    dicts -- the caller (app.py's auto-cycle) resends `message` for
    each, then calls record_success()/record_failure() based on the
    outcome, same as a fresh attempt."""
    now = now or dt.datetime.now()
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM intelligence_alert_retry_state WHERE exhausted = 0").fetchall()
    finally:
        conn.close()
    return [
        {"identity": r["identity"], "message": r["message"], "attempt_count": r["attempt_count"]}
        for r in rows if _is_row_due(r, now)
    ]
