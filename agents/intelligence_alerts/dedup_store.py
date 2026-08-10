"""
agents/intelligence_alerts/dedup_store.py -- Milestone 15, Phase 1:
Alert Deduplication & Cooldown Protection. Own isolated table namespace
(intelligence_alert_dedup_state), CREATE TABLE IF NOT EXISTS only --
same DB_PATH/_connect()/init_db() shape every other store.py in this
package already uses. Real SQLite file (not in-memory), so this state
survives a process restart the same way every other table in this
database already does -- no extra persistence work needed.

One row per (symbol, bias, rule) -- this is CURRENT dedup state (what
was the last confidence bucket sent, and when), not an append-only log;
store.py's intelligence_alerts_log already is the audit trail of what
was actually delivered.

should_suppress() is this module's one real decision: given a fresh
evaluation, is this a repeat of the SAME-OR-LESSER market condition
within its cooldown window, or a genuinely new/escalated one? The
lookup key deliberately omits the confidence bucket (unlike
cooldown.make_fingerprint()'s own, coarser key) so a bucket INCREASE
can be compared against the last-sent bucket and bypass suppression
even inside the cooldown window, while a same-or-lower bucket cannot --
exactly the Phase 1 spec's own bypass rules:
  - bias changes            -> different (symbol, bias, rule) row -> not suppressed
  - trigger type changes    -> different (symbol, bias, rule) row -> not suppressed
  - confidence bucket rises -> same row, but rank increased        -> not suppressed
  - confidence bucket same/lower, still within cooldown            -> suppressed
"""
import datetime as dt
import logging
import sqlite3

from . import cooldown as cooldown_logic

DB_PATH = "oi_history.db"
log = logging.getLogger("intelligence_alerts.dedup")


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
            CREATE TABLE IF NOT EXISTS intelligence_alert_dedup_state (
                condition_key   TEXT PRIMARY KEY,
                symbol          TEXT NOT NULL,
                bias            TEXT NOT NULL,
                rule            TEXT NOT NULL,
                bucket_rank     INTEGER NOT NULL,
                last_sent_at    TEXT NOT NULL
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _condition_key(symbol: str, bias: str, rule: str) -> str:
    return f"{symbol}|{bias}|{rule}"


def _get(condition_key: str):
    conn = _connect()
    try:
        return conn.execute(
            "SELECT * FROM intelligence_alert_dedup_state WHERE condition_key = ?", (condition_key,)
        ).fetchone()
    finally:
        conn.close()


def _upsert(condition_key: str, symbol: str, bias: str, rule: str, bucket_rank: int, now_iso: str) -> None:
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO intelligence_alert_dedup_state "
            "(condition_key, symbol, bias, rule, bucket_rank, last_sent_at) "
            "VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(condition_key) DO UPDATE SET "
            "bucket_rank=excluded.bucket_rank, last_sent_at=excluded.last_sent_at",
            (condition_key, symbol, bias, rule, bucket_rank, now_iso),
        )
        conn.commit()
    finally:
        conn.close()


def should_suppress(*, symbol: str, bias: str, confidence, rule: str, cooldown_seconds: int, now=None) -> bool:
    """True if this exact market condition (or a lesser one) already
    sent an alert within cooldown_seconds and nothing has genuinely
    changed; False (recording this as the new baseline) otherwise.
    Logs ALERT_SENT or ALERT_SUPPRESSED_DUPLICATE with the full
    fingerprint (symbol|bias|bucket|rule) and remaining cooldown, per
    the Phase 1 spec's structured-logging requirement."""
    now = now or dt.datetime.now()
    bucket = cooldown_logic.confidence_bucket(confidence)
    rank = cooldown_logic.bucket_rank(bucket)
    fingerprint = cooldown_logic.make_fingerprint(symbol=symbol, bias=bias, confidence=confidence, rule=rule)
    condition_key = _condition_key(symbol, bias, rule)

    row = _get(condition_key)
    if row is not None:
        elapsed = (now - dt.datetime.fromisoformat(row["last_sent_at"])).total_seconds()
        remaining = cooldown_seconds - elapsed
        if remaining > 0 and rank <= row["bucket_rank"]:
            log.info(f"ALERT_SUPPRESSED_DUPLICATE fingerprint={fingerprint!r} remaining_cooldown={remaining:.0f}s")
            return True

    _upsert(condition_key, symbol, bias, rule, rank, now.isoformat())
    log.info(f"ALERT_SENT fingerprint={fingerprint!r} remaining_cooldown=0s")
    return False
