"""
agents/intelligence_alerts/threshold_store.py -- Milestone 14, Phase 3:
persisted, operator-configurable overrides for this package's own alert
thresholds. A simple key-value table -- CREATE TABLE IF NOT EXISTS
only, own isolated namespace, matching every other store.py in this
project.

Every key here has a hardcoded default in agents/config.py -- an
override row here takes precedence over that default when present;
get_effective_config() falls back to the code default when no override
exists. This means:
- A fresh install with an empty table behaves EXACTLY as Milestone 14
  Phase 1/2 already did -- zero behavior change until an operator
  actually sets something.
- Every threshold stays documented in agents/config.py's own comments
  even after being made overridable -- this table only ever stores
  DEVIATIONS from that documented default, never becomes the sole
  place a value is defined.

No write function here validates business rules (sane bounds, known
symbol names, etc.) -- that happens in agents/intelligence_alerts/
api.py, the layer that first receives operator input, matching how
agents.trading_intelligence.ti_store.open_trade() doesn't validate
recommendation logic either. This module only ever persists what it's
given for a KNOWN key.
"""
import datetime as dt
import json
import sqlite3

from agents import config as agents_config

DB_PATH = "oi_history.db"

# Every overridable key, and the function that reads its documented
# default from agents/config.py -- called lazily (not at import time)
# so a test/operator monkeypatching agents_config still sees the
# override take effect.
_DEFAULTS = {
    "confidence_window": lambda: agents_config.INTELLIGENCE_ALERT_CONFIDENCE_WINDOW,
    "confidence_stdev_threshold": lambda: agents_config.INTELLIGENCE_ALERT_CONFIDENCE_STDEV_THRESHOLD,
    "oi_window": lambda: agents_config.INTELLIGENCE_ALERT_OI_WINDOW,
    "auto_cooldown_seconds": lambda: agents_config.INTELLIGENCE_ALERTS_AUTO_COOLDOWN_SECONDS,
    "low_liquidity_suppression_symbols": lambda: list(agents_config.INTELLIGENCE_ALERT_LOW_LIQUIDITY_SUPPRESSION_SYMBOLS),
    # Milestone 15, Phase 0: Bias Flip Stabilization -- see agents/config.py's
    # own comment on these two constants for the full rationale.
    "min_bias_confirmations": lambda: agents_config.INTELLIGENCE_ALERT_MIN_BIAS_CONFIRMATIONS,
    "bias_flip_cooldown_seconds": lambda: agents_config.INTELLIGENCE_ALERT_BIAS_FLIP_COOLDOWN_SECONDS,
    # Milestone 15, Phase 1: Alert Deduplication & Cooldown Protection --
    # see agents/config.py's own comment on this constant.
    "dedup_cooldown_seconds": lambda: agents_config.INTELLIGENCE_ALERT_DEDUP_COOLDOWN_SECONDS,
}
VALID_KEYS = tuple(_DEFAULTS.keys())


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
            CREATE TABLE IF NOT EXISTS intelligence_alert_thresholds (
                key         TEXT PRIMARY KEY,
                value_json  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                updated_by  TEXT NOT NULL,
                reason      TEXT
            );
            """
        )
        conn.commit()
    finally:
        conn.close()


def _now() -> str:
    return dt.datetime.now().isoformat()


def get_override_rows() -> list:
    """Every currently-set override, full rows (including who/when/why)
    -- for the read-only /rules route to show real provenance, not just
    an effective number. Empty list on a fresh install."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT key, value_json, updated_at, updated_by, reason "
            "FROM intelligence_alert_thresholds ORDER BY key"
        ).fetchall()
    finally:
        conn.close()
    return [
        {"key": r["key"], "value": json.loads(r["value_json"]), "updated_at": r["updated_at"],
         "updated_by": r["updated_by"], "reason": r["reason"]}
        for r in rows
    ]


def get_effective_config() -> dict:
    """Every threshold's CURRENT effective value -- the override if one
    exists, else the agents.config default. This is what rules.py
    actually reads."""
    overrides = {row["key"]: row["value"] for row in get_override_rows()}
    return {key: overrides.get(key, default_fn()) for key, default_fn in _DEFAULTS.items()}


def set_override(key: str, value, *, updated_by: str, reason: str) -> None:
    if key not in VALID_KEYS:
        raise ValueError(f"unknown threshold key: {key!r} (valid: {VALID_KEYS})")
    conn = _connect()
    try:
        conn.execute(
            "INSERT INTO intelligence_alert_thresholds (key, value_json, updated_at, updated_by, reason) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET "
            "value_json=excluded.value_json, updated_at=excluded.updated_at, "
            "updated_by=excluded.updated_by, reason=excluded.reason",
            (key, json.dumps(value), _now(), updated_by, reason),
        )
        conn.commit()
    finally:
        conn.close()


def clear_override(key: str) -> None:
    """Removes an override -- that key reverts to its agents.config
    default on the very next read. A no-op (not an error) if the key
    was never overridden."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM intelligence_alert_thresholds WHERE key=?", (key,))
        conn.commit()
    finally:
        conn.close()
