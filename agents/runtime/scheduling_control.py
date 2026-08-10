"""
agents/runtime/scheduling_control.py -- Milestone 12, Phase 2
Foundation: Agent Scheduling Controls.

Two things this module owns, deliberately kept separate from each other
because they answer two different questions:

1. **Is this agent even eligible to be scheduled, ever?** (`SCHEDULABLE_
   AGENTS` / `is_schedulable()`) -- a hard, code-level constant, NOT a
   database row. `quant_researcher` and `shadow_mode` are permanently
   excluded here (quant_researcher's real-world cost has never been
   measured; shadow_mode is locked on the same permanent footing even
   though its own cycle function is read-only -- see NEVER_SCHEDULABLE_
   AGENTS' own comment). This is intentionally NOT stored in the
   database specifically so "cannot be scheduled even if explicitly
   requested" holds even against a bad row, a bug, or a future caller
   who doesn't know better -- a mutable flag could always be flipped by
   something; a module-level frozenset checked in code cannot be
   overridden by any operator action this module itself exposes.

   Milestone 12, Phase 2A originally excluded `trading_intelligence`
   here too (it opens real paper trades with no policy gate reaching
   it -- agents.runtime.policy_engine is never imported anywhere under
   agents/trading_intelligence/). Milestone 17 deliberately removed it
   from this set -- see NEVER_SCHEDULABLE_AGENTS' own comment for the
   two remaining operational conditions (RUNTIME_SCHEDULER_ENABLED,
   schedule_mode) that still gate it actually running, and for why this
   is a code change only, not itself an activation.

2. **For an agent that IS schedulable, what mode is it in right now?**
   (`get_mode()`/`set_mode()`) -- "enabled" (runs normally), "disabled"
   (never runs), "dry_run" (the scheduler logs "would have run" and
   moves on, without invoking the agent's cycle function at all -- NOT
   Phase 2A Shadow Mode execution, which this milestone's own scope
   explicitly excludes from this phase; dry_run here is scheduling
   metadata/observability only, never a partial or simulated real
   execution). Persisted in agent_status (extends the SAME table
   Milestone 9's orchestrator.py and agent_runtime.py already use, via
   sysadmin_store's own self-migrating column pattern) -- so it survives
   a process restart, unlike an in-memory flag.

Deliberately NOT built as an extension of agents.sys_admin.orchestrator.py
(Milestone 8's own enable/disable, scoped to exactly four Milestone 2-7
agents) -- agent_runtime.py's own module docstring already establishes
the precedent for why a new M12-scoped runtime concern gets a new,
purpose-built module rather than editing a previous milestone's file:
"trading_intelligence... deliberately NOT added to orchestrator.AGENT_NAMES
-- that module is explicitly scoped to four Milestone 2-7 agents."
Scheduling mode here applies uniformly to every schedulable agent
(including memory/sys_admin, neither of which orchestrator.py covers at
all), so it gets its own module rather than stretching orchestrator's
own historical scope.

Every mode change is audited two ways, matching this framework's own
established "every autonomous-adjacent action logs who/why" convention
(the same one policy_engine.set_policy() already holds to): a
sysadmin_report (queryable via sysadmin_store.list_reports()) and a
best-effort AGENT_MODE_CHANGED runtime event.
"""
import logging

from ..sys_admin import sysadmin_report, sysadmin_store
from . import agent_runtime, runtime_events

logger = logging.getLogger("oi_dashboard.runtime.scheduling_control")

# Hard, code-level exclusion -- see module docstring. Never sourced from
# the database, never configurable via set_mode() below (set_mode()
# raises for any agent in this set, on purpose).
#
# "shadow_mode" (Milestone 12, Phase 3): registered in RUNTIME_AGENT_NAMES
# for status/health tracking only -- its cycle function is a read-only
# heartbeat that never executes real observation/evaluation logic (see
# agent_runtime._shadow_mode_cycle()'s own docstring), but it is locked
# here anyway, on the same permanent, code-level footing as
# quant_researcher, so it can never be scheduled under any circumstance
# regardless of what its cycle function might ever be changed to do in
# the future.
#
# Milestone 17: "trading_intelligence" deliberately REMOVED from this
# set -- the explicit, confirmed objective of M17 is to make it
# schedulable for continuous unattended execution. This is a CODE
# change only; it does not itself start anything. Two more conditions,
# both operational (not code), still gate real execution:
#   1. agents.config.RUNTIME_SCHEDULER_ENABLED must be true (env var,
#      off by default -- a deploy-time/.env change, separate from this
#      code change).
#   2. get_mode("trading_intelligence") must resolve to ENABLED. As of
#      this change, no agent_status row exists yet for
#      trading_intelligence in production (verified directly against
#      the live database) -- get_mode()'s own documented behavior is
#      "never-run agents are schedulable by default," so once this
#      code is deployed AND RUNTIME_SCHEDULER_ENABLED is true, it
#      would start running on its next due cycle with NO separate
#      "enable" step, unless an explicit schedule_mode is set first
#      (see runtime_control_cli.py's own set-mode command -- "disabled"
#      or "dry_run" are both safe ways to hold it back deliberately
#      once unblocked here, before committing to "enabled").
# Still excludes broker execution / real-money order placement itself
# -- nothing here, or anywhere agents.trading_intelligence imports,
# changes that boundary; this only concerns whether the scheduler is
# permitted to CALL trading_intelligence's own already-existing,
# already-tested cycle function on a timer.
NEVER_SCHEDULABLE_AGENTS = frozenset({"quant_researcher", "shadow_mode"})

SCHEDULABLE_AGENTS = tuple(a for a in agent_runtime.RUNTIME_AGENT_NAMES if a not in NEVER_SCHEDULABLE_AGENTS)

ENABLED = "enabled"
DISABLED = "disabled"
DRY_RUN = "dry_run"
VALID_MODES = (ENABLED, DISABLED, DRY_RUN)


def is_schedulable(agent: str) -> bool:
    """Pure, code-level check -- never touches the database. This is
    what agents.runtime.scheduler._due_agents() consults first, before
    anything else, so trading_intelligence/quant_researcher can never
    reach the rest of the due-agent logic regardless of any stored
    state."""
    return agent in SCHEDULABLE_AGENTS


def get_mode(agent: str) -> str:
    """"disabled" for a never-schedulable agent, always -- regardless of
    whatever schedule_mode a stray/legacy database row might contain
    (belt-and-suspenders on top of is_schedulable(), so a caller that
    only checks get_mode() without also checking is_schedulable() still
    gets the safe answer). "enabled" (the honest default) for a
    schedulable agent with no status row yet -- never-run agents are
    schedulable by default, matching agent_runtime.py's own "never run
    yet" defaults elsewhere."""
    if agent not in SCHEDULABLE_AGENTS:
        return DISABLED
    status = sysadmin_store.get_agent_status(agent)
    if status is None:
        return ENABLED
    return status.get("schedule_mode") or ENABLED


def set_mode(agent: str, mode: str, *, changed_by: str, reason: str):
    """The one function that changes an agent's scheduling mode. Raises
    ValueError (never silently no-ops) for:
    - `agent` not in SCHEDULABLE_AGENTS -- this is the literal
      enforcement of "trading_intelligence/quant_researcher cannot be
      scheduled even if explicitly requested": no mode, including
      "enabled", is ever accepted for either.
    - `mode` not one of VALID_MODES.

    Every successful change is recorded as a sysadmin_report (queryable,
    durable, matches every other operator-adjacent action in this
    framework) and best-effort emitted as AGENT_MODE_CHANGED -- the
    mode change itself (persisted via sysadmin_store.upsert_agent_status,
    a real database write) always succeeds even if the event emission
    doesn't."""
    if agent not in SCHEDULABLE_AGENTS:
        raise ValueError(
            f"{agent!r} can never be scheduled (agents.runtime.scheduling_control.NEVER_SCHEDULABLE_AGENTS) -- "
            f"refusing to set a schedule_mode for it under any circumstance"
        )
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {VALID_MODES}, got {mode!r}")

    sysadmin_store.upsert_agent_status(agent, schedule_mode=mode)
    report = sysadmin_report.build(
        module="scheduling_control", action="set_mode",
        reason=f"{agent} schedule_mode set to {mode!r} by {changed_by}: {reason}",
        confidence=100, evidence={"agent": agent, "mode": mode, "changed_by": changed_by, "reason": reason},
        affected_components=[agent],
        severity="warning" if mode == DISABLED else "info",
    )
    sysadmin_store.record_report(report)
    runtime_events.emit_safe(
        "scheduling_control", runtime_events.AGENT_MODE_CHANGED,
        {"agent": agent, "mode": mode, "changed_by": changed_by, "reason": reason},
        severity="warning" if mode == DISABLED else "info",
    )
    return report


def snapshot() -> dict:
    """Every agent agent_runtime.RUNTIME_AGENT_NAMES knows about, keyed
    by name -- the read-only status data source for /api/runtime/status
    (see agents.runtime.lifecycle.get_runtime_status()). Never raises;
    reuses sysadmin_store.get_agent_status()'s own honest "no row yet"
    handling for schedulable agents that have never had their mode
    touched."""
    out = {}
    for agent in agent_runtime.RUNTIME_AGENT_NAMES:
        out[agent] = {"schedulable": is_schedulable(agent), "mode": get_mode(agent)}
    return out
