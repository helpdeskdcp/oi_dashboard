"""
agents/sys_admin/orchestrator.py -- "Register agents automatically.
Start/Stop agents. Restart crashed agents. Heartbeat monitoring. Agent
dependency graph."

This framework's agents (Milestones 2-7) are trigger-invoked function
modules, not long-running OS daemons -- there is deliberately no
scheduler anywhere in this codebase (AUTONOMOUS_AGENTS_ARCHITECTURE.md:
"Do not run on a timer"). "Start/Stop" therefore means an enable/disable
flag; "Restart" means clearing a recorded crash so the next real trigger
is allowed through again -- it never automatically re-invokes the
operation that crashed (that could re-trigger a costly/risky LLM call,
backtest, or research cycle without a human ever deciding to. See
agents/sys_admin/self_healing.py's docstring for the fuller safety
rationale this whole package holds to).
"""
import datetime as dt

from ..trading_supervisor import agent_health
from . import sysadmin_report, sysadmin_store

AGENT_NAMES = ("dev_agent", "quant_researcher", "risk_manager", "trading_supervisor")
# Memory isn't a triggered agent (no enable/disable makes sense for a
# passive store) so it's excluded from AGENT_NAMES but still covered by
# agent_health.memory_health() in dependency/health reporting below.

# Real data dependencies each agent's own entrypoint actually has (not
# inferred -- read directly off what Milestones 3-7 built): dev_agent/
# quant_researcher/risk_manager all search agents.memory before acting;
# trading_supervisor's Gate 7 reads memory (conflict detection) plus the
# real outcome of dev_agent/quant_researcher/risk_manager's own gates.
DEPENDENCY_GRAPH = {
    "dev_agent": ("memory",),
    "quant_researcher": ("memory",),
    "risk_manager": ("memory",),
    "trading_supervisor": ("memory", "dev_agent", "quant_researcher", "risk_manager"),
}


def is_enabled(agent: str) -> bool:
    status = sysadmin_store.get_agent_status(agent)
    return True if status is None else bool(status["enabled"])


def enable_agent(agent: str) -> None:
    sysadmin_store.upsert_agent_status(agent, enabled=True)


def disable_agent(agent: str, *, reason: str) -> None:
    sysadmin_store.upsert_agent_status(agent, enabled=False)
    sysadmin_store.record_report(sysadmin_report.build(
        module="orchestrator", action="disable_agent", reason=reason, confidence=100,
        evidence={"agent": agent}, affected_components=[agent], severity="warning",
    ))


def heartbeat(agent: str) -> None:
    """Called by an agent's own real entrypoint at the start of a run.
    Not yet wired into every agent's entrypoint in this milestone (a
    deliberate, documented scope boundary -- see AI_SYSTEM_ADMINISTRATOR.md);
    orchestrator.py's heartbeat/staleness machinery is complete and
    tested independent of that wiring, which is additive follow-up work
    per agent."""
    sysadmin_store.upsert_agent_status(agent, last_heartbeat_ts=dt.datetime.now().isoformat())


def record_crash(agent: str, *, reason: str) -> None:
    sysadmin_store.upsert_agent_status(agent, crashed=True, crash_reason=reason)
    sysadmin_store.record_report(sysadmin_report.build(
        module="orchestrator", action="record_crash", reason=reason, confidence=90,
        evidence={"agent": agent}, affected_components=[agent], severity="critical",
    ))


def restart_agent(agent: str):
    """"Restart" for a trigger-invoked agent: clears the crashed flag so
    the next real trigger is allowed through again. Deliberately does
    NOT re-invoke the agent's own entrypoint -- that decision (and its
    cost/risk) belongs to whatever normally triggers this agent, not to
    an automatic restart. Safe to call unconditionally (a no-op report
    if the agent wasn't marked crashed)."""
    status = sysadmin_store.get_agent_status(agent)
    was_crashed = bool(status and status.get("crashed"))
    sysadmin_store.upsert_agent_status(agent, crashed=False, crash_reason=None)
    report = sysadmin_report.build(
        module="orchestrator", action="restart_agent",
        reason=f"cleared crashed state for {agent}" if was_crashed else f"{agent} was not marked crashed",
        confidence=100, evidence={"agent": agent, "was_crashed": was_crashed},
        affected_components=[agent],
        recovery_outcome="crashed flag cleared; next trigger will run normally",
        severity="info",
    )
    sysadmin_store.record_report(report)
    return report


def dependency_health(agent: str, memory_store) -> dict:
    """For each dependency this agent has, is it healthy right now?
    Reuses agents.trading_supervisor.agent_health (Milestone 7) rather
    than a second health-check implementation."""
    result = {}
    for dep in DEPENDENCY_GRAPH.get(agent, ()):
        if dep == "memory":
            h = agent_health.memory_health(memory_store)
            result[dep] = {"healthy": h.reachable and not h.is_stale}
        else:
            h = agent_health.agent_activity_health(dep)
            result[dep] = {"healthy": not h.is_failing}
    return result


def registry_snapshot(memory_store) -> dict:
    """One call, the full orchestration picture: every known agent's
    enabled/crashed/heartbeat state plus its dependency health."""
    snapshot = {}
    for agent in AGENT_NAMES:
        status = sysadmin_store.get_agent_status(agent) or {
            "agent": agent, "enabled": 1, "last_heartbeat_ts": None, "crashed": 0, "crash_reason": None,
        }
        snapshot[agent] = {**status, "dependencies": dependency_health(agent, memory_store)}
    return snapshot
