"""
agents/intelligence_alerts/api.py -- Milestone 14: read-only
aggregation functions backing the GET-only /api/intelligence/alerts/*
routes in app.py (store.py's own SELECT-only helpers, or
threshold_store's effective-config merge -- nothing here writes) plus,
since Phase 3, the two validated write functions
(set_threshold()/clear_threshold()) backing the one POST route,
/api/intelligence/alerts/config.

Telegram/email "configured" below means the required env vars are
present -- NOT that a real send has succeeded. Checked directly via
os.getenv rather than importing app.py, which this package deliberately
never imports (see intelligence_alerts_cli.py's own docstring for why).
"""
import os

from agents import config as agents_config

from . import store, threshold_store

# Milestone 14, Phase 3: sane bounds for each overridable threshold --
# checked here, the layer that first receives operator input, not in
# threshold_store.py itself (which only ever persists what it's given
# for a known key, matching how ti_store.open_trade() doesn't validate
# business rules either). A bad value here raises ValueError, which the
# route below turns into a 400 -- never silently clamped or ignored.
_BOUNDS = {
    "confidence_window": (int, 2, 100),
    "confidence_stdev_threshold": (float, 0, None),
    "oi_window": (int, 2, 100),
    "auto_cooldown_seconds": (int, 0, None),
    "min_bias_confirmations": (int, 1, 20),
    "bias_flip_cooldown_seconds": (int, 0, None),
    "dedup_cooldown_seconds": (int, 0, None),
}


def get_status() -> dict:
    return {
        "mode": "intelligence_alerts",
        "read_only": True,
        "no_orders_placed": True,
        "alert_count": store.count_total(),
        "last_alert_ts": store.last_alert_ts(),
        "telegram_configured": bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")),
        "email_configured": bool(agents_config.INTELLIGENCE_ALERT_EMAIL_TO),
    }


def get_recent_page(*, symbol: str | None = None, limit: int = 10, offset: int = 0) -> dict:
    return {
        "items": store.list_recent(symbol=symbol, limit=limit, offset=offset),
        "total": store.count_total(symbol=symbol),
        "limit": limit,
        "offset": offset,
    }


def get_rules() -> dict:
    """Read-only dump of the ACTIVE (effective) threshold config -- an
    override if one has been set via set_threshold() below, else the
    agents/config.py default. Milestone 14, Phase 3: also carries an
    "overrides" key -- the raw override rows (who/when/why), so this
    stays a real audit view, not just a snapshot of numbers. This route
    itself is never gated by INTELLIGENCE_ALERT_CONFIG_API_ENABLED --
    only the write route is, same split RUNTIME_CONTROL_API_ENABLED's
    own precedent already established for /api/runtime/status vs.
    /api/runtime/control/*."""
    config = threshold_store.get_effective_config()
    return {
        "bias_flip": {
            "min_confirmations": config["min_bias_confirmations"],
            "cooldown_seconds": config["bias_flip_cooldown_seconds"],
        },
        "confidence_unstable": {
            "window": config["confidence_window"],
            "stdev_threshold": config["confidence_stdev_threshold"],
        },
        "greeks_incoherent": "alerts when the latest snapshot's bias and greeks_alignment disagree",
        "oi_non_responsive": {
            "window": config["oi_window"],
            "low_liquidity_suppression_symbols": config["low_liquidity_suppression_symbols"],
        },
        "auto_cooldown_seconds": config["auto_cooldown_seconds"],
        "dedup_cooldown_seconds": config["dedup_cooldown_seconds"],
        "overrides": threshold_store.get_override_rows(),
    }


def _validate(key: str, value) -> None:
    if key not in threshold_store.VALID_KEYS:
        raise ValueError(f"unknown threshold key: {key!r} (valid: {threshold_store.VALID_KEYS})")

    if key == "low_liquidity_suppression_symbols":
        if not isinstance(value, list) or not all(isinstance(s, str) and s.strip() for s in value):
            raise ValueError("low_liquidity_suppression_symbols must be a list of non-empty strings")
        if len(set(value)) != len(value):
            raise ValueError("low_liquidity_suppression_symbols must not contain duplicates")
        return

    expected_type, lo, hi = _BOUNDS[key]
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"{key} must be a number, got {type(value).__name__}")
    if lo is not None and value < lo:
        raise ValueError(f"{key}={value} is below the minimum allowed ({lo})")
    if hi is not None and value > hi:
        raise ValueError(f"{key}={value} is above the maximum allowed ({hi})")


def set_threshold(key: str, value, *, updated_by: str, reason: str) -> dict:
    """Validates, then persists. Raises ValueError on an invalid key or
    an out-of-bounds value -- the route turns that into a 400. Returns
    the new full effective config (get_rules()'s own shape) so a caller
    sees the actual result, not just an "ok"."""
    _validate(key, value)
    if key == "low_liquidity_suppression_symbols":
        value = [s.strip().upper() for s in value]
    threshold_store.set_override(key, value, updated_by=updated_by, reason=reason)
    return get_rules()


def clear_threshold(key: str, *, updated_by: str, reason: str) -> dict:
    """Reverts one key to its agents/config.py default. updated_by/
    reason aren't persisted for a clear (there's no row left to attach
    them to) -- they exist in this signature so the route's audit
    log.info() call has the same shape for both set and clear."""
    if key not in threshold_store.VALID_KEYS:
        raise ValueError(f"unknown threshold key: {key!r} (valid: {threshold_store.VALID_KEYS})")
    threshold_store.clear_override(key)
    return get_rules()
