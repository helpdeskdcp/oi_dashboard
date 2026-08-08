"""
agents/runtime/runtime_events.py -- "Create a real internal event
system." Extends agents/event_bus.py (Milestone 1: publish() to
agent_events, poll-based via events_since() -- no broker, matching this
codebase's own "two SQLite tables are the event bus" design principle)
with the full event taxonomy the runtime layer needs, rather than a
second event system. Every event type below is just a agent_events
row with a specific event_type string; nothing here changes
agent_events' schema or agents.event_bus's own functions.
"""
import logging

from .. import event_bus

logger = logging.getLogger("oi_dashboard.runtime.runtime_events")

# The exact taxonomy requested, as event_type strings.
MARKET_OPEN = "market_open"
MARKET_CLOSE = "market_close"
NEW_CANDLE = "new_candle"
NEW_TICK = "new_tick"
STRATEGY_UPDATED = "strategy_updated"
RISK_ALERT = "risk_alert"
MEMORY_UPDATED = "memory_updated"
PATCH_GENERATED = "patch_generated"
BACKTEST_FINISHED = "backtest_finished"
BROKER_CONNECTED = "broker_connected"
BROKER_DISCONNECTED = "broker_disconnected"
DATABASE_FAILURE = "database_failure"
RECOVERY_COMPLETED = "recovery_completed"

# Runtime-internal events (not in the requested list verbatim, but
# needed for the scheduler/workflow/approval machinery itself to be
# observable the same way -- "every autonomous action must log reason/
# evidence," extended here to "every runtime transition is an event").
WORKFLOW_STAGE_ADVANCED = "workflow_stage_advanced"
WORKFLOW_WAITING_APPROVAL = "workflow_waiting_approval"
WORKFLOW_COMPLETED = "workflow_completed"
WORKFLOW_FAILED = "workflow_failed"
APPROVAL_GRANTED = "approval_granted"
APPROVAL_REJECTED = "approval_rejected"
POLICY_CHANGED = "policy_changed"
SCHEDULER_STARTED = "scheduler_started"
SCHEDULER_STOPPED = "scheduler_stopped"
AGENT_CYCLE_FAILED = "agent_cycle_failed"
AGENT_ESCALATED = "agent_escalated"

# Milestone 12, Phase 1: emitted when RuntimeScheduler.tick()'s own
# non-agent code (task_queue.process_one/workflow_engine.advance) raises
# -- distinct from AGENT_CYCLE_FAILED (which is per-agent, already
# isolated inside agent_runtime.run_agent_cycle and never propagates to
# tick() at all). "Recovered" because tick() catches it and the
# scheduler loop keeps running -- this event exists purely for
# observability of that recovery, not because anything failed
# unrecoverably.
SCHEDULER_TICK_RECOVERED = "scheduler_tick_recovered"

# Milestone 12, Phase 2 Foundation: emitted by agents.runtime.
# scheduling_control.set_mode() every time an operator changes an
# agent's schedule_mode (enabled/disabled/dry_run) -- the per-agent
# counterpart to POLICY_CHANGED (which is global, one policy for the
# whole scheduler).
AGENT_MODE_CHANGED = "agent_mode_changed"

ALL_EVENT_TYPES = (
    MARKET_OPEN, MARKET_CLOSE, NEW_CANDLE, NEW_TICK, STRATEGY_UPDATED, RISK_ALERT,
    MEMORY_UPDATED, PATCH_GENERATED, BACKTEST_FINISHED, BROKER_CONNECTED, BROKER_DISCONNECTED,
    DATABASE_FAILURE, RECOVERY_COMPLETED, WORKFLOW_STAGE_ADVANCED, WORKFLOW_WAITING_APPROVAL,
    WORKFLOW_COMPLETED, WORKFLOW_FAILED, APPROVAL_GRANTED, APPROVAL_REJECTED, POLICY_CHANGED,
    SCHEDULER_STARTED, SCHEDULER_STOPPED, AGENT_CYCLE_FAILED, AGENT_ESCALATED, SCHEDULER_TICK_RECOVERED,
    AGENT_MODE_CHANGED,
)

# Severity a caller doesn't have to think about for the common case --
# still overridable via the severity= kwarg on emit() for a specific
# occurrence that's worse than its type's default (e.g. a
# database_failure that also lost unbacked-up data).
_DEFAULT_SEVERITY = {
    RISK_ALERT: "critical", DATABASE_FAILURE: "critical", AGENT_ESCALATED: "critical",
    BROKER_DISCONNECTED: "warning", WORKFLOW_FAILED: "warning", AGENT_CYCLE_FAILED: "warning",
    APPROVAL_REJECTED: "warning", SCHEDULER_TICK_RECOVERED: "warning",
}


def emit(source_agent: str, event_type: str, payload: dict, *, severity: str | None = None) -> int:
    """Thin, typed wrapper over agents.event_bus.publish() -- validates
    event_type is a known one (a typo here should fail loudly, not
    silently create an unqueryable event type no subscriber will ever
    match) and fills in a sensible default severity."""
    if event_type not in ALL_EVENT_TYPES:
        raise ValueError(f"unknown runtime event_type {event_type!r} -- add it to ALL_EVENT_TYPES first")
    return event_bus.publish(
        source_agent=source_agent, event_type=event_type, payload=payload,
        severity=severity or _DEFAULT_SEVERITY.get(event_type, "info"),
    )


def emit_safe(source_agent: str, event_type: str, payload: dict, *, severity: str | None = None) -> None:
    """Milestone 12, Phase 1.1 originally established this exact pattern
    as a private helper inside scheduler.py (_safe_emit) -- promoted here
    as a shared utility now that Phase 2 Foundation needs the same
    guarantee from policy_engine.py and scheduling_control.py too: a
    failure to write the agent_events table (real on a database that
    hasn't been initialized yet, or under any other storage failure)
    must never propagate into an operator's own control-plane action.
    Pausing the scheduler, or disabling a misbehaving agent, must always
    succeed even if the audit-trail write about it fails -- the action
    itself is never allowed to be silently blocked by its own logging."""
    try:
        emit(source_agent, event_type, payload, severity=severity)
    except Exception:
        logger.exception("failed to emit a %r runtime event -- continuing without it", event_type)


def poll(since_ts: str, *, event_types: tuple | None = None) -> list:
    """Poll-based, matching agents.event_bus.events_since()'s own
    design (no push callbacks, no broker) -- optionally filtered to a
    subset of event types, since events_since() itself only filters to
    one at a time. Used by scheduler.py's event-driven execution: poll
    since the last tick, react to whatever's new."""
    if event_types is None:
        return event_bus.events_since(since_ts)
    seen = []
    for et in event_types:
        seen.extend(event_bus.events_since(since_ts, event_type=et))
    seen.sort(key=lambda e: e["ts"])
    return seen
