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
import signal
import time

from .. import config, memory
from ..sys_admin import sysadmin_report, sysadmin_store
from . import (
    agent_runtime,
    market_session,
    policy_engine,
    runtime_events,
    runtime_store,
    task_queue,
    workflow_engine,
)

_MARKET_SESSION_GATED_AGENTS = ("quant_researcher", "trading_supervisor")


class RuntimeScheduler:
    def __init__(self, *, repo_dir: str = ".", memory_store=None, tick_interval_seconds: float = 5.0):
        self._repo_dir = repo_dir
        self._memory_store = memory_store or memory.get_memory_store()
        self._tick_interval_seconds = tick_interval_seconds
        self._running = False
        self._task_handlers: dict = {}

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
        even across a scheduler restart."""
        self._running = True
        resumed = 0
        for wf in runtime_store.resumable_workflows():
            if wf["status"] == "running":
                workflow_engine.resume(wf["id"], memory_store=self._memory_store, repo_dir=self._repo_dir)
                resumed += 1
        report = sysadmin_report.build(
            module="scheduler", action="start", reason=f"scheduler started, {resumed} workflow(s) resumed",
            confidence=100, evidence={"resumed_workflow_count": resumed}, severity="info",
        )
        sysadmin_store.record_report(report)
        runtime_events.emit("scheduler", runtime_events.SCHEDULER_STARTED, {"resumed_workflow_count": resumed})

    def stop(self) -> None:
        """"Graceful shutdown": stops the loop after the CURRENT tick
        finishes -- never kills an in-flight agent cycle or workflow
        stage transition mid-way (every stage transition is already
        persisted before the next one starts, so there is nothing "mid-
        way" to lose even in the worst case, but this still waits for
        the current tick rather than tearing in immediately)."""
        self._running = False
        runtime_events.emit("scheduler", runtime_events.SCHEDULER_STOPPED, {})

    def install_signal_handlers(self) -> None:
        """Only called from the real production entrypoint (run_forever
        via __main__), never from tests -- registering process-wide
        signal handlers inside a test process would leak across tests."""
        def _handle(signum, frame):
            self.stop()
        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)

    # --- one iteration ---------------------------------------------------

    def _due_agents(self) -> list:
        market_open, _reason = market_session.is_nse_session_open()
        due = []
        for agent in agent_runtime.RUNTIME_AGENT_NAMES:
            if agent in _MARKET_SESSION_GATED_AGENTS and not market_open:
                continue
            status = sysadmin_store.get_agent_status(agent)
            cadence = config.RUNTIME_CADENCE_SECONDS.get(agent, 300)
            if status is None or status.get("last_execution_ts") is None:
                due.append(agent)
                continue
            last = dt.datetime.fromisoformat(status["last_execution_ts"])
            if (dt.datetime.now() - last).total_seconds() >= cadence:
                due.append(agent)
        return due

    def tick(self) -> dict:
        """One full scheduler iteration: runs every due agent, drains
        one queued task, advances every in-flight workflow, and returns
        a summary dict -- fully synchronous and fully testable."""
        if policy_engine.is_emergency_stop():
            return {"emergency_stop": True}

        agents_run = []
        for agent in self._due_agents():
            result = agent_runtime.run_agent_cycle(agent, memory_store=self._memory_store, repo_dir=self._repo_dir)
            agents_run.append(result)

        handlers = {"dev_agent_trigger": lambda payload: None}  # drained inside agent_runtime's own dev_agent cycle
        handlers.update(self._task_handlers)
        task_result = task_queue.process_one(handlers) if self._task_handlers else None

        workflows_advanced = []
        for wf in runtime_store.list_workflows(status="running", limit=100):
            new_status = workflow_engine.advance(wf["id"], memory_store=self._memory_store, repo_dir=self._repo_dir)
            workflows_advanced.append({"workflow_id": wf["id"], "status": new_status})

        return {
            "agents_run": agents_run, "task_result": task_result, "workflows_advanced": workflows_advanced,
            "queue_depth": task_queue.status(),
        }

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

    def run_forever(self) -> None:
        """The real production entrypoint: "runs continuously." A plain
        while loop + time.sleep -- no new always-on infrastructure
        (matches AUTONOMOUS_AGENTS_ARCHITECTURE.md's own design
        principle #3). When the market is closed, sleeps until the next
        session open (market_session.seconds_until_next_open) instead of
        polling every tick_interval_seconds for nothing -- still runs
        sys_admin/memory/risk_manager on their own cadence regardless
        (see _due_agents/_MARKET_SESSION_GATED_AGENTS), so this only
        skips the busy-poll, never the always-on health checks."""
        self.install_signal_handlers()
        self.start()
        try:
            while self._running:
                self.tick()
                market_open, _reason = market_session.is_nse_session_open()
                if not market_open:
                    time.sleep(min(self._tick_interval_seconds * 12, market_session.seconds_until_next_open()))
                else:
                    time.sleep(self._tick_interval_seconds)
        finally:
            self.stop()


if __name__ == "__main__":
    RuntimeScheduler().run_forever()
