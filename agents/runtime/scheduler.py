"""
agents/runtime/scheduler.py -- "Create a production scheduler that:
starts automatically, runs continuously, supports market sessions,
supports configurable intervals, supports event-driven execution,
supports graceful shutdown."

The one piece of code in this entire framework that actually calls each
agent's cycle unattended, closing AUTONOMOUS_READINESS_REPORT.md's #1
finding. Every call it makes is to an already-tested entrypoint
(agents.runtime.agent_runtime.run_agent_cycle, agents.runtime.task_queue.
process_one, agents.runtime.workflow_engine.advance) -- this module's own
job is only sequencing and timing, never decision logic.

Testable without an actual infinite loop: tick() runs exactly one
iteration (what every test in test_agents/runtime/test_scheduler.py
calls directly); run_forever() is the real, continuously-running
production entrypoint (a plain `while` loop + `time.sleep`, no new
threading/async runtime -- matching this codebase's "no new always-on
infrastructure" design principle) and is not exercised by the test
suite itself, only smoke-tested for a couple of bounded iterations via
run_for().
"""
import datetime as dt
import logging
import signal
import time

from .. import config, memory
from ..sys_admin import sysadmin_report, sysadmin_store
from . import (
    agent_runtime,
    heartbeat,
    market_session,
    metrics,
    policy_engine,
    runtime_events,
    runtime_store,
    scheduling_control,
    task_queue,
    workflow_engine,
)

logger = logging.getLogger("oi_dashboard.runtime.scheduler")

_MARKET_SESSION_GATED_AGENTS = ("quant_researcher", "trading_supervisor", "trading_intelligence")

# Milestone 12, Phase 1: how often (in ticks) run_forever() logs an
# INFO-level "still alive" heartbeat line. Every tick would be too noisy
# at the default 5s cadence (17,280 lines/day); this is a genuine
# liveness signal for anyone tailing logs, not the same thing as the
# per-cycle DEBUG line tick() itself always emits (see tick()'s own
# docstring) or the queryable /api/runtime/status endpoint.
HEARTBEAT_LOG_EVERY_N_CYCLES = 12


def _safe_emit(source_agent: str, event_type: str, payload: dict) -> None:
    """Milestone 12, Phase 1.1 hotfix: runtime_events.emit() is pure
    observability (a write to the agent_events table) -- a failure to
    write it must never propagate into the scheduler's own control flow.
    Real, reproduced failure mode this fixes: a genuinely uninitialized
    database (agent_events table doesn't exist yet -- true for this
    project's own live database as of Milestone 12 Phase 1's own
    post-merge verification) used to make tick()'s exception-RECOVERY
    path raise a SECOND exception while trying to record the first one,
    escaping tick() entirely and killing run_forever()'s loop -- the
    exact opposite of what that recovery path exists for. Every
    runtime_events.emit() call inside this module's lifecycle/recovery
    code goes through this wrapper instead of calling emit() directly.

    Milestone 12, Phase 2 Foundation: this same guarantee is now also
    needed by policy_engine.py/scheduling_control.py, so the actual
    try/except now lives once, shared, as runtime_events.emit_safe() --
    this function is kept as a thin, backward-compatible name so every
    existing call site/docstring reference in this module is unchanged."""
    runtime_events.emit_safe(source_agent, event_type, payload)


class RuntimeScheduler:
    def __init__(self, *, repo_dir: str = ".", memory_store=None, tick_interval_seconds: float = 5.0):
        self._repo_dir = repo_dir
        self._memory_store = memory_store or memory.get_memory_store()
        self._tick_interval_seconds = tick_interval_seconds
        self._running = False
        self._task_handlers: dict = {}
        # Milestone 12, Phase 1: scheduler-level (not per-agent -- see
        # agent_runtime.health_snapshot() for that) runtime metrics,
        # exposed via get_status(). "stopped" is the initial/idle state,
        # distinct from "never started" only in that this instance simply
        # hasn't had start()/run_forever() called on it yet.
        self._state = "stopped"
        self._cycles_executed = 0
        self._recovered_exceptions = 0
        self._last_cycle_ts: dt.datetime | None = None
        self._last_cycle_duration_ms: float | None = None
        self._started_at: dt.datetime | None = None
        # Milestone 15, Phase 3: Runtime Scheduler Observability.
        self._heartbeat = heartbeat.CycleHeartbeat()
        self._duration_metrics = metrics.RunningAverage()

    def register_task_handler(self, task_type: str, handler) -> None:
        """Extension point for task_type strings beyond dev_agent_trigger
        (agent_runtime.py's own handler for that one is wired in tick()
        automatically) -- e.g. a future integration enqueuing its own
        task_type the scheduler should know how to run."""
        self._task_handlers[task_type] = handler

    # --- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """"Starts automatically": called once before the first tick.
        Resumes every workflow runtime_store.resumable_workflows() finds
        left running/waiting_approval -- "never lose workflow state,"
        even across a scheduler restart.

        Milestone 12, Phase 1.1 hotfix: the workflow-resume sweep and the
        startup sysadmin report are both best-effort bookkeeping, not
        core to "the scheduler is now ticking" -- on a genuinely
        uninitialized database (no runtime_workflow/agent_status table
        yet), these used to raise here, escape start(), and (since
        start() runs inside run_forever()'s own try block) kill the
        scheduler loop before it ever reached its first tick. Each is now
        independently isolated so a missing table degrades to "resumed
        0 workflows" / "no report recorded" instead of preventing
        startup entirely."""
        self._state = "starting"
        self._running = True
        self._started_at = dt.datetime.now()
        resumed = 0
        try:
            for wf in runtime_store.resumable_workflows():
                if wf["status"] == "running":
                    workflow_engine.resume(wf["id"], memory_store=self._memory_store, repo_dir=self._repo_dir)
                    resumed += 1
        except Exception:
            logger.exception("failed to resume in-flight workflows at startup -- continuing without it")

        try:
            report = sysadmin_report.build(
                module="scheduler", action="start", reason=f"scheduler started, {resumed} workflow(s) resumed",
                confidence=100, evidence={"resumed_workflow_count": resumed}, severity="info",
            )
            sysadmin_store.record_report(report)
        except Exception:
            logger.exception("failed to record the scheduler-start sysadmin report -- continuing without it")

        _safe_emit("scheduler", runtime_events.SCHEDULER_STARTED, {"resumed_workflow_count": resumed})
        self._state = "running"

    def stop(self) -> None:
        """"Graceful shutdown": stops the loop after the CURRENT tick
        finishes -- never kills an in-flight agent cycle or workflow
        stage transition mid-way (every stage transition is already
        persisted before the next one starts, so there is nothing "mid-
        way" to lose even in the worst case, but this still waits for
        the current tick rather than tearing in immediately)."""
        self._state = "stopping"
        self._running = False
        _safe_emit("scheduler", runtime_events.SCHEDULER_STOPPED, {})

    def install_signal_handlers(self) -> None:
        """Only called from the real production entrypoint (run_forever
        via __main__), never from tests -- registering process-wide
        signal handlers inside a test process would leak across tests."""
        def _handle(signum, frame):
            self.stop()
        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)

    # --- one iteration ---------------------------------------------------

    def _is_due_by_cadence(self, agent: str, *, market_open: bool) -> bool:
        """The original per-agent due-ness check (market-hours gate +
        cadence-elapsed), factored out so _due_agents() and
        _dry_run_due_agents() (Milestone 12, Phase 2 Foundation) share
        one implementation instead of two copies of the same logic."""
        if agent in _MARKET_SESSION_GATED_AGENTS and not market_open:
            return False
        status = sysadmin_store.get_agent_status(agent)
        cadence = config.RUNTIME_CADENCE_SECONDS.get(agent, 300)
        if status is None or status.get("last_execution_ts") is None:
            return True
        last = dt.datetime.fromisoformat(status["last_execution_ts"])
        return (dt.datetime.now() - last).total_seconds() >= cadence

    def _due_agents(self) -> list:
        """Agents whose cycle tick() will actually invoke this iteration
        -- return shape (a flat list of agent names) is UNCHANGED from
        before Milestone 12, Phase 2 Foundation; existing callers/tests
        are unaffected. Now additionally filtered through
        scheduling_control: trading_intelligence/quant_researcher can
        never appear here -- scheduling_control.is_schedulable() is
        checked first, before anything else, since that's a hard,
        code-level exclusion (see that module's own docstring for why
        it's deliberately not a database flag). A "disabled" agent is
        skipped; a "dry_run" agent that's otherwise due is reported by
        _dry_run_due_agents() instead, never executed from here."""
        market_open, _reason = market_session.is_nse_session_open()
        due = []
        for agent in agent_runtime.RUNTIME_AGENT_NAMES:
            if not scheduling_control.is_schedulable(agent):
                continue
            if scheduling_control.get_mode(agent) in (scheduling_control.DISABLED, scheduling_control.DRY_RUN):
                continue
            if self._is_due_by_cadence(agent, market_open=market_open):
                due.append(agent)
        return due

    def _dry_run_due_agents(self) -> list:
        """Milestone 12, Phase 2 Foundation: schedulable, dry_run-mode
        agents that WOULD be due right now. tick() logs these as "would
        have run" and does not call agent_runtime.run_agent_cycle() for
        any of them -- scheduling metadata/observability only, never
        Phase 2A Shadow Mode execution (this milestone's own scope
        explicitly excludes that from this phase; no agent cycle logic
        runs for a dry-run agent, the same as a disabled one)."""
        market_open, _reason = market_session.is_nse_session_open()
        due = []
        for agent in agent_runtime.RUNTIME_AGENT_NAMES:
            if not scheduling_control.is_schedulable(agent):
                continue
            if scheduling_control.get_mode(agent) != scheduling_control.DRY_RUN:
                continue
            if self._is_due_by_cadence(agent, market_open=market_open):
                due.append(agent)
        return due

    def tick(self) -> dict:
        """One full scheduler iteration: runs every due agent, drains
        one queued task, advances every in-flight workflow, and returns
        a summary dict -- fully synchronous and fully testable.

        Milestone 12, Phase 1: never raises, and always updates this
        instance's cycle metrics (cycles_executed/last_cycle_ts/
        last_cycle_duration_ms -- see get_status()), regardless of
        outcome. Per-agent failures were ALREADY isolated (agent_runtime.
        run_agent_cycle() never raises); what's new here is isolating
        THIS method's own non-agent code (task_queue.process_one(),
        workflow_engine.advance()) -- an exception there used to
        propagate straight out of tick() and would have killed
        run_forever()'s loop. Recovered exceptions are counted
        (_recovered_exceptions) and best-effort emitted as a
        SCHEDULER_TICK_RECOVERED event for observability (via
        _safe_emit() -- Milestone 12, Phase 1.1 hotfix: the emit itself
        must never be able to defeat this recovery, e.g. on a database
        that doesn't have the agent_events table yet), then the
        scheduler keeps running regardless of whether that event write
        succeeded.

        Milestone 12, Phase 2 Foundation: `dry_run_agents` in the
        returned dict lists any schedule_mode="dry_run" agent that was
        due this tick -- logged (DEBUG) and reported, never executed
        (agent_runtime.run_agent_cycle() is not called for them, the
        same as a disabled agent)."""
        start = time.perf_counter()
        try:
            if policy_engine.is_emergency_stop():
                result = {"emergency_stop": True}
            else:
                agents_run = []
                for agent in self._due_agents():
                    agent_result = agent_runtime.run_agent_cycle(
                        agent, memory_store=self._memory_store, repo_dir=self._repo_dir,
                    )
                    agents_run.append(agent_result)

                dry_run_agents = self._dry_run_due_agents()
                for agent in dry_run_agents:
                    logger.debug("dry-run: %r was due this tick but was not executed (schedule_mode=dry_run)", agent)

                # drained inside agent_runtime's own dev_agent cycle
                handlers = {"dev_agent_trigger": lambda payload: None}
                handlers.update(self._task_handlers)
                task_result = task_queue.process_one(handlers) if self._task_handlers else None

                workflows_advanced = []
                for wf in runtime_store.list_workflows(status="running", limit=100):
                    new_status = workflow_engine.advance(
                        wf["id"], memory_store=self._memory_store, repo_dir=self._repo_dir,
                    )
                    workflows_advanced.append({"workflow_id": wf["id"], "status": new_status})

                result = {
                    "agents_run": agents_run, "dry_run_agents": dry_run_agents, "task_result": task_result,
                    "workflows_advanced": workflows_advanced, "queue_depth": task_queue.status(),
                }
        except Exception as exc:
            self._recovered_exceptions += 1
            _safe_emit(
                "scheduler", runtime_events.SCHEDULER_TICK_RECOVERED,
                {"error": str(exc), "recovered_exceptions": self._recovered_exceptions},
            )
            result = {"error": str(exc), "recovered": True}

        self._cycles_executed += 1
        self._last_cycle_ts = dt.datetime.now()
        self._last_cycle_duration_ms = round((time.perf_counter() - start) * 1000, 2)
        self._duration_metrics.add(self._last_cycle_duration_ms)
        # Milestone 15, Phase 3: Runtime Scheduler Observability. Same
        # success/failure signal tick()'s own exception-recovery above
        # already computed (an "error" key means the try block's own
        # exception-recovery path was hit) -- a per-agent failure
        # inside agents_run does NOT count as a tick failure here, since
        # those are already independently isolated/reported by
        # agent_runtime.run_agent_cycle() and don't represent scheduler-
        # level instability.
        if "error" in result:
            self._heartbeat.record_failure(self._last_cycle_ts)
        else:
            self._heartbeat.record_success(self._last_cycle_ts)
        logger.debug(
            "tick #%d completed in %.2fms (state=%s)", self._cycles_executed, self._last_cycle_duration_ms, self._state,
        )
        return result

    def get_status(self) -> dict:
        """Scheduler-level runtime metrics -- Milestone 12 Phase 1's
        /api/runtime/status endpoint's own data source (see
        agents.runtime.lifecycle.get_runtime_status(), which adds
        "active_jobs" from agent_runtime.health_snapshot() on top of
        this). "next_scheduled_cycle" is honestly the next time THIS
        SCHEDULER LOOP wakes up to check for due work (last_cycle_ts +
        tick_interval_seconds) -- not a per-agent prediction, since
        individual agents run on their own independently staggered
        cadences (see agent_runtime.RUNTIME_AGENT_NAMES/config.
        RUNTIME_CADENCE_SECONDS) and there is no single honest "next
        agent cycle" to report."""
        uptime_seconds = None
        if self._started_at is not None:
            uptime_seconds = round((dt.datetime.now() - self._started_at).total_seconds(), 1)
        next_scheduled_cycle = None
        if self._last_cycle_ts is not None:
            next_scheduled_cycle = (
                self._last_cycle_ts + dt.timedelta(seconds=self._tick_interval_seconds)
            ).isoformat()
        status = {
            "scheduler_state": self._state,
            "cycles_executed": self._cycles_executed,
            "recovered_exceptions": self._recovered_exceptions,
            "last_cycle_timestamp": self._last_cycle_ts.isoformat() if self._last_cycle_ts else None,
            "next_scheduled_cycle": next_scheduled_cycle,
            "last_cycle_duration_ms": self._last_cycle_duration_ms,
            "runtime_uptime_seconds": uptime_seconds,
            # Milestone 15, Phase 3: Runtime Scheduler Observability.
            "average_cycle_duration_ms": self._duration_metrics.average,
        }
        status.update(self._heartbeat.to_dict())
        return status

    # --- run loops -------------------------------------------------------

    def run_for(self, *, iterations: int, sleep_seconds: float | None = None) -> list:
        """Bounded loop -- what tests and a CLI smoke-run use. Never an
        infinite `while True`."""
        sleep_seconds = self._tick_interval_seconds if sleep_seconds is None else sleep_seconds
        results = []
        for i in range(iterations):
            results.append(self.tick())
            if i < iterations - 1 and sleep_seconds:
                time.sleep(sleep_seconds)
        return results

    def run_forever(self, *, install_signal_handlers: bool = True) -> None:
        """The real production entrypoint: "runs continuously." A plain
        while loop + time.sleep -- no new always-on infrastructure
        (matches AUTONOMOUS_AGENTS_ARCHITECTURE.md's own design
        principle #3). When the market is closed, sleeps until the next
        session open (market_session.seconds_until_next_open) instead of
        polling every tick_interval_seconds for nothing -- still runs
        sys_admin/memory/risk_manager on their own cadence regardless
        (see _due_agents/_MARKET_SESSION_GATED_AGENTS), so this only
        skips the busy-poll, never the always-on health checks.

        `install_signal_handlers` (Milestone 12, Phase 1): defaults to
        True, preserving this method's original behavior for its only
        prior caller (this module's own __main__ block, which always
        runs on the main thread). agents.runtime.lifecycle -- which runs
        this method on a BACKGROUND thread inside the live Flask process
        -- passes False and installs the signal handlers itself on the
        main thread instead, since Python's signal.signal() raises if
        called from any thread but the main one."""
        if install_signal_handlers:
            self.install_signal_handlers()
        try:
            self.start()
            logger.info("scheduler run_forever() started (pid loop entered, state=%s)", self._state)
            try:
                while self._running:
                    self.tick()
                    if self._cycles_executed % HEARTBEAT_LOG_EVERY_N_CYCLES == 0:
                        logger.info(
                            "scheduler heartbeat: %d cycles executed, %d recovered exception(s), "
                            "last cycle %.2fms, uptime %.0fs",
                            self._cycles_executed, self._recovered_exceptions,
                            self._last_cycle_duration_ms or 0.0, self.get_status()["runtime_uptime_seconds"] or 0.0,
                        )
                    market_open, _reason = market_session.is_nse_session_open()
                    if not market_open:
                        time.sleep(min(self._tick_interval_seconds * 12, market_session.seconds_until_next_open()))
                    else:
                        time.sleep(self._tick_interval_seconds)
            finally:
                self.stop()
                self._state = "stopped"
                logger.info("scheduler run_forever() loop exited cleanly (state=%s)", self._state)
        except Exception:
            self._state = "error"
            logger.exception("scheduler run_forever() crashed out of its loop unexpectedly")
            raise


if __name__ == "__main__":
    RuntimeScheduler().run_forever()
