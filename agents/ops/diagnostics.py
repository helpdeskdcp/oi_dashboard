"""
agents/ops/diagnostics.py -- Milestone 16, Phase 2: Health Snapshot &
Diagnostics Bundle. Pure composition -- reads already-computed state
from agents.runtime (scheduler/lifecycle), agents.intelligence_alerts
(dedup/rate-limit/delivery counts), and this package's own event_log;
writes nothing anywhere.

Unlike agents.runtime.lifecycle's own documented narrow-dependency
boundary ("never imports agents.trading_intelligence directly"), this
module's entire purpose is cross-cutting operational visibility, so
importing from both agents.runtime and agents.intelligence_alerts here
is intentional, not a boundary violation -- agents/ops/ exists
specifically to observe across other packages.

Never includes a raw secret/credential value anywhere in either bundle
below -- only boolean "is X configured" flags, matching the exact
pattern agents.intelligence_alerts.api.get_status()'s own
telegram_configured/email_configured already established. Read via
os.getenv() directly (never by importing app.py), same reason that
module gives for doing the same.
"""
import os

from agents import config as agents_config
from agents.intelligence_alerts import dedup_store, rate_limiter, store as alerts_store, threshold_store
from agents.runtime import lifecycle

from . import event_log, models


def _alert_summary() -> dict:
    return {
        "sent": alerts_store.count_delivered_telegram(),
        "suppressed": dedup_store.count_suppressions(),
        "rate_limited": rate_limiter.count_rate_limited(),
    }


def build_alerts_summary() -> dict:
    """GET /api/ops/alerts/summary's own data source -- Milestone 16,
    Phase 4. "suppressed" and "deduplicated" are deliberately the SAME
    number: "suppressed" already means dedup_store.count_suppressions()
    in this codebase (see _alert_summary() above, shipped in Phase 2)
    -- there is no separate, distinct "deduplicated" concept here to
    give a different count to, so giving it a fabricated different
    value would be dishonest, and giving "suppressed" a DIFFERENT
    meaning here than it already has in /api/runtime/health-snapshot
    would be an inconsistent API. Both keys are still present, exactly
    as the spec asked for, just honestly equal.

    "retried"/"failed" are the actually-distinct real counts: every
    RETRY_SCHEDULED ops event is one retry attempt scheduled;
    RETRY_EXHAUSTED is a delivery that permanently gave up (distinct
    from "sent" being merely attempted-but-not-yet-delivered, which
    isn't a final failure)."""
    deduplicated = dedup_store.count_suppressions()
    return {
        "sent": alerts_store.count_delivered_telegram(),
        "suppressed": deduplicated,
        "deduplicated": deduplicated,
        "rate_limited": rate_limiter.count_rate_limited(),
        "retried": event_log.count_events(event_type=models.RETRY_SCHEDULED),
        "failed": event_log.count_events(event_type=models.RETRY_EXHAUSTED),
    }


def _active_cooldowns(*, now=None) -> list:
    cooldown_seconds = threshold_store.get_effective_config()["dedup_cooldown_seconds"]
    return dedup_store.get_active_conditions(cooldown_seconds=cooldown_seconds, now=now)


def _non_secret_config_summary() -> dict:
    """Booleans and non-sensitive thresholds only -- NEVER a raw
    credential/token value, an API key, or a database path. Every
    value here is either already public-safe elsewhere in this app
    (e.g. RUNTIME_SCHEDULER_ENABLED is already shown at
    /api/runtime/status) or a plain configured/not-configured flag."""
    return {
        "telegram_configured": bool(os.getenv("TELEGRAM_BOT_TOKEN") and os.getenv("TELEGRAM_CHAT_ID")),
        "email_configured": bool(agents_config.INTELLIGENCE_ALERT_EMAIL_TO),
        "runtime_scheduler_enabled": agents_config.RUNTIME_SCHEDULER_ENABLED,
        "runtime_control_api_enabled": agents_config.RUNTIME_CONTROL_API_ENABLED,
        "intelligence_alerts_auto_enabled": agents_config.INTELLIGENCE_ALERTS_AUTO_ENABLED,
        "runtime_circuit_failure_threshold": agents_config.RUNTIME_CIRCUIT_FAILURE_THRESHOLD,
        "runtime_circuit_recovery_seconds": agents_config.RUNTIME_CIRCUIT_RECOVERY_SECONDS,
        "ops_event_retention_days": agents_config.OPS_EVENT_RETENTION_DAYS,
        "environment": agents_config.ENVIRONMENT,
    }


def build_health_snapshot() -> dict:
    """GET /api/runtime/health-snapshot's own data source -- a
    consolidated operational view: scheduler status/heartbeat/metrics/
    circuit state (all already in agents.runtime.lifecycle.
    get_runtime_status()), recent ops events, an alert-delivery
    summary, and how many conditions are currently in an active
    cooldown."""
    status = lifecycle.get_runtime_status()
    return {
        "scheduler": status,
        "recent_events": event_log.get_events(limit=20),
        "alert_summary": _alert_summary(),
        "active_cooldown_count": len(_active_cooldowns()),
    }


def build_diagnostics_bundle() -> dict:
    """GET /api/runtime/diagnostics.json's own data source -- a fuller,
    downloadable export: the same runtime status, a compact metrics
    snapshot, the last 50 events, every currently-active cooldown
    FINGERPRINT (not just a count, unlike the health snapshot above),
    circuit-breaker state, and a non-secret configuration summary."""
    status = lifecycle.get_runtime_status()
    return {
        "runtime_status": status,
        "metrics_snapshot": {
            "cycles_executed": status.get("cycles_executed"),
            "average_cycle_duration_ms": status.get("average_cycle_duration_ms"),
            "last_cycle_duration_ms": status.get("last_cycle_duration_ms"),
            "recovered_exceptions": status.get("recovered_exceptions"),
            "consecutive_failures": status.get("consecutive_failures"),
        },
        "recent_events": event_log.get_events(limit=50),
        "active_cooldowns": [c["condition_key"] for c in _active_cooldowns()],
        "circuit_breaker": {
            "state": status.get("circuit_state"),
            "consecutive_failures": status.get("circuit_consecutive_failures"),
        },
        "config_summary": _non_secret_config_summary(),
    }
