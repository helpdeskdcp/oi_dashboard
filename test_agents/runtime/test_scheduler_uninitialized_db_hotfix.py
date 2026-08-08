"""
test_agents/runtime/test_scheduler_uninitialized_db_hotfix.py --
Milestone 12, Phase 1.1 hotfix regression tests.

Every other test in this package uses the agent_db/ti_db fixtures, which
-- reasonably, for isolated unit testing -- call every relevant
init_db() up front. That's precisely why the bug this hotfix fixes went
undetected by the 22 tests Phase 1 shipped with: none of them reproduce
the real database's actual state, where agents/runtime and agents/
sys_admin's own tables (agent_status, agent_events, runtime_policy,
runtime_workflow, ...) have never been created (confirmed directly
against the live oi_history.db during Phase 1's own post-merge
verification).

The `uninitialized_db` fixture below deliberately does the OPPOSITE of
agent_db: it points every relevant module at a real, fresh SQLite file
and calls NO init_db() on any of them -- reproducing the actual
production condition byte-for-byte, not a hypothetical one.
"""
import threading
import time

import pytest

from agents.runtime import agent_runtime, lifecycle
from agents.runtime import runtime_events as re
from agents.runtime import scheduler as scheduler_module
from agents.runtime import task_queue, workflow_engine
from agents.runtime.scheduler import RuntimeScheduler


@pytest.fixture()
def uninitialized_db(monkeypatch, tmp_path):
    """A real SQLite file with ZERO tables -- no agent_status, no
    agent_events, no runtime_policy, no runtime_workflow, nothing. This
    is what agents.runtime/agents.sys_admin's own tables actually look
    like against this project's real oi_history.db today."""
    from agents import audit_log, event_bus
    from agents.runtime import runtime_store
    from agents.sys_admin import sysadmin_store

    db_path = str(tmp_path / "genuinely_empty.db")
    monkeypatch.setattr(sysadmin_store, "DB_PATH", db_path)
    monkeypatch.setattr(runtime_store, "DB_PATH", db_path)
    monkeypatch.setattr(event_bus, "DB_PATH", db_path)
    monkeypatch.setattr(audit_log, "DB_PATH", db_path)
    # Deliberately: no .init_db() call on any of the above.
    return db_path


@pytest.fixture()
def _reset_lifecycle_globals():
    saved_scheduler, saved_lock = lifecycle._scheduler, lifecycle._lock_file
    lifecycle._scheduler, lifecycle._lock_file = None, None
    yield
    if lifecycle._scheduler is not None:
        lifecycle._scheduler.stop()
    if lifecycle._lock_file is not None:
        lifecycle._release_singleton_lock(lifecycle._lock_file)
    lifecycle._scheduler, lifecycle._lock_file = saved_scheduler, saved_lock


class TestMissingAgentEventsTable:
    """Scope item 3, bullet 1: missing agent_events table."""

    def test_safe_emit_never_raises_when_agent_events_table_is_missing(self, uninitialized_db):
        # Must not raise -- this is the literal bug: emit() alone used to.
        scheduler_module._safe_emit("scheduler", re.SCHEDULER_STARTED, {"resumed_workflow_count": 0})

    def test_direct_runtime_events_emit_does_raise_without_the_fix(self, uninitialized_db):
        """Sanity check that the fixture genuinely reproduces the bug's
        precondition -- if this ever stops raising (e.g. a future change
        auto-creates the table), _safe_emit's own test above would no
        longer be exercising anything real."""
        with pytest.raises(Exception):
            re.emit("scheduler", re.SCHEDULER_STARTED, {"resumed_workflow_count": 0})


class TestTickRaisesAndRecovers:
    """Scope item 3, bullets 2 and 3: tick() raising an exception, and
    the scheduler continuing to execute subsequent cycles -- against a
    genuinely uninitialized database, not a partially-set-up one."""

    def test_tick_does_not_raise_against_a_fully_uninitialized_database(self, uninitialized_db):
        sched = RuntimeScheduler(repo_dir=".")
        result = sched.tick()  # must not raise
        assert result.get("recovered") is True
        assert sched.get_status()["recovered_exceptions"] == 1
        assert sched.get_status()["cycles_executed"] == 1

    def test_scheduler_keeps_executing_subsequent_cycles(self, uninitialized_db):
        sched = RuntimeScheduler(repo_dir=".", tick_interval_seconds=0)
        results = sched.run_for(iterations=5, sleep_seconds=0)
        assert len(results) == 5
        assert all(r.get("recovered") is True for r in results)
        assert sched.get_status()["cycles_executed"] == 5
        assert sched.get_status()["recovered_exceptions"] == 5

    def test_start_does_not_raise_against_a_fully_uninitialized_database(self, uninitialized_db):
        """The deeper half of this hotfix: start() itself (workflow-resume
        sweep + sysadmin report + emit) used to be able to raise BEFORE
        the scheduler ever reached its first tick, which run_forever()'s
        own outer except would catch by killing the whole loop -- the
        scheduler could not even begin operating against a fresh
        database. start() must complete and reach "running.\""""
        sched = RuntimeScheduler(repo_dir=".")
        sched.start()  # must not raise
        assert sched.get_status()["scheduler_state"] == "running"

    def test_run_forever_survives_a_fully_uninitialized_database_on_a_background_thread(self, uninitialized_db):
        sched = RuntimeScheduler(repo_dir=".", tick_interval_seconds=0.05)
        thread = threading.Thread(target=sched.run_forever, kwargs={"install_signal_handlers": False}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10.0
            while sched.get_status()["cycles_executed"] < 3 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert sched.get_status()["cycles_executed"] >= 3
            assert sched.get_status()["scheduler_state"] == "running"
        finally:
            sched.stop()
            thread.join(timeout=10.0)
        assert not thread.is_alive()
        assert sched.get_status()["scheduler_state"] == "stopped"


class TestRuntimeStatusDegradesSafely:
    """Scope item 3, bullet 4: /api/runtime/status returning a safe
    degraded response instead of a 500 error."""

    def test_get_runtime_status_never_raises_with_no_scheduler_and_no_tables(
        self, uninitialized_db, _reset_lifecycle_globals,
    ):
        status = lifecycle.get_runtime_status()  # must not raise
        assert status["scheduler_state"] == "stopped"
        assert status["active_jobs"] is None  # honestly unknown, never a fabricated 0

    def test_get_runtime_status_never_raises_with_a_running_scheduler(
        self, uninitialized_db, _reset_lifecycle_globals,
    ):
        sched = RuntimeScheduler(repo_dir=".", tick_interval_seconds=0)
        sched.start()
        sched.tick()
        lifecycle._scheduler = sched
        status = lifecycle.get_runtime_status()  # must not raise
        assert status["scheduler_state"] == "running"
        assert status["cycles_executed"] == 1
        assert status["active_jobs"] is None

    def test_direct_health_snapshot_does_raise_without_the_fix(self, uninitialized_db):
        """Sanity check the fixture genuinely reproduces the bug's
        precondition for this surface too."""
        with pytest.raises(Exception):
            agent_runtime.health_snapshot()


class TestStartupWithSchedulerEnabled:
    """Scope item 2's third bullet: startup with the scheduler enabled
    must not break against an uninitialized database."""

    def test_start_scheduler_background_does_not_raise_and_scheduler_reaches_running(
        self, uninitialized_db, monkeypatch, tmp_path, _reset_lifecycle_globals,
    ):
        monkeypatch.setattr(lifecycle.config, "RUNTIME_SCHEDULER_ENABLED", True)
        monkeypatch.setattr(lifecycle.config, "RUNTIME_SCHEDULER_LOCK_PATH", str(tmp_path / "sched.lock"))

        started = lifecycle.start_scheduler_background(
            task_starter=lambda func, *a, **kw: threading.Thread(
                target=func, args=a, kwargs=kw, daemon=True,
            ).start(),
            tick_interval_seconds=0.05,
        )
        assert started is True
        try:
            deadline = time.monotonic() + 10.0
            while lifecycle.get_runtime_status()["scheduler_state"] != "running" and time.monotonic() < deadline:
                time.sleep(0.05)
            assert lifecycle.get_runtime_status()["scheduler_state"] == "running"
        finally:
            lifecycle.stop_scheduler_background()
