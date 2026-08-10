"""
agents/ops/models.py -- Milestone 16, Phase 1: Persistent Runtime
Event Log. Event-type taxonomy for agents/ops/event_log.py's own
dedicated table -- deliberately NOT agents.event_bus's agent_events
table (see event_log.py's own module docstring for why: agent_events
holds audit-significant governance events -- policy changes, approvals,
escalations -- that must never be subject to a casual time-based
retention purge; these are high-volume OPERATIONAL telemetry events,
exactly the kind of thing a 30-day retention window is safe to apply
to).
"""

SCHEDULER_STARTED = "scheduler_started"
SCHEDULER_STOPPED = "scheduler_stopped"
HEARTBEAT_UPDATED = "heartbeat_updated"
ALERT_SENT = "alert_sent"
ALERT_SUPPRESSED = "alert_suppressed"
RATE_LIMIT_HIT = "rate_limit_hit"
RETRY_SCHEDULED = "retry_scheduled"
RETRY_EXHAUSTED = "retry_exhausted"
CIRCUIT_OPENED = "circuit_opened"
CIRCUIT_HALF_OPEN = "circuit_half_open"
CIRCUIT_CLOSED = "circuit_closed"
WATCHDOG_STALE_CYCLE = "watchdog_stale_cycle"

ALL_EVENT_TYPES = (
    SCHEDULER_STARTED, SCHEDULER_STOPPED, HEARTBEAT_UPDATED, ALERT_SENT, ALERT_SUPPRESSED,
    RATE_LIMIT_HIT, RETRY_SCHEDULED, RETRY_EXHAUSTED, CIRCUIT_OPENED, CIRCUIT_HALF_OPEN,
    CIRCUIT_CLOSED, WATCHDOG_STALE_CYCLE,
)
