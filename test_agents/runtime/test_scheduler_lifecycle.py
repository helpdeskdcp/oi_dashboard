"""
test_agents/runtime/test_scheduler_lifecycle.py -- Milestone 12, Phase 1:
Runtime Scheduler Activation. Covers the new scheduler-level metrics/
state tracking and tick()-level exception isolation added to
RuntimeScheduler, plus agents.runtime.lifecycle's activation/singleton-
lock/status-reporting behavior. Never calls run_forever() with real
sleep intervals or against the real oi_history.db -- see the `agent_db`/
`ti_db`/`memory_store` fixtures (test_agents/conftest.py, test_agents/
runtime/conftest.py) already established for this whole package.
"""
import logging
import threading
import time

import pytest

from agents.runtime import lifecycle
from agents.runtime import runtime_store as rs
from agents.runtime import scheduler as scheduler_module
from agents.runtime.scheduler import RuntimeScheduler


class TestSchedulerStatus:
    def test_initial_status_is_honestly_idle(self, agent_db, memory_store):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        status = sched.get_status()
        assert status["scheduler_state"] == "stopped"
        assert status["cycles_executed"] == 0
        assert status["recovered_exceptions"] == 0
        assert status["last_cycle_timestamp"] is None
        assert status["next_scheduled_cycle"] is None
        assert status["last_cycle_duration_ms"] is None
        assert status["runtime_uptime_seconds"] is None

    def test_start_transitions_to_running_and_sets_uptime(self, agent_db, memory_store):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        sched.start()
        status = sched.get_status()
        assert status["scheduler_state"] == "running"
        assert status["runtime_uptime_seconds"] is not None
        assert status["runtime_uptime_seconds"] >= 0.0

    def test_tick_updates_cycle_metrics(self, agent_db, memory_store):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        sched.tick()
        status = sched.get_status()
        assert status["cycles_executed"] == 1
        assert status["last_cycle_timestamp"] is not None
        assert status["last_cycle_duration_ms"] is not None
        assert status["last_cycle_duration_ms"] >= 0.0

    def test_multiple_ticks_accumulate_the_counter(self, agent_db, memory_store):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store, tick_interval_seconds=0)
        sched.run_for(iterations=3, sleep_seconds=0)
        assert sched.get_status()["cycles_executed"] == 3

    def test_next_scheduled_cycle_is_last_cycle_plus_tick_interval(self, agent_db, memory_store):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store, tick_interval_seconds=42.0)
        sched.tick()
        status = sched.get_status()
        import datetime as dt
        last = dt.datetime.fromisoformat(status["last_cycle_timestamp"])
        next_cycle = dt.datetime.fromisoformat(status["next_scheduled_cycle"])
        assert round((next_cycle - last).total_seconds(), 1) == 42.0

    def test_stop_transitions_to_stopping(self, agent_db, memory_store):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        sched.start()
        sched.stop()
        assert sched.get_status()["scheduler_state"] == "stopping"


class TestTickExceptionIsolation:
    """The plan's own explicit resilience requirement: an exception in
    tick()'s non-agent code (task_queue/workflow_engine) must not
    propagate and must not stop the scheduler from being usable again."""

    def test_a_workflow_engine_exception_is_recovered_not_raised(self, agent_db, memory_store, monkeypatch):
        from agents.runtime import workflow_engine as wf

        rs.create_workflow(
            workflow_type="promotion", symbol="NIFTY", first_stage=wf.STAGE_MARKET_DATA, state={"symbol": "NIFTY"},
        )

        def _boom(*a, **kw):
            raise RuntimeError("simulated workflow_engine failure")

        monkeypatch.setattr(wf, "advance", _boom)

        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        result = sched.tick()  # must not raise

        assert result.get("recovered") is True
        assert "simulated workflow_engine failure" in result["error"]
        assert sched.get_status()["recovered_exceptions"] == 1
        assert sched.get_status()["cycles_executed"] == 1  # still counted as a real cycle

    def test_scheduler_keeps_working_after_a_recovered_exception(self, agent_db, memory_store, monkeypatch):
        from agents.runtime import workflow_engine as wf

        rs.create_workflow(
            workflow_type="promotion", symbol="NIFTY", first_stage=wf.STAGE_MARKET_DATA, state={"symbol": "NIFTY"},
        )
        call_count = {"n": 0}
        real_advance = wf.advance

        def _boom_once(*a, **kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("simulated one-time failure")
            return real_advance(*a, **kw)

        monkeypatch.setattr(wf, "advance", _boom_once)

        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        first = sched.tick()
        assert first.get("recovered") is True

        # No new "running" workflow exists for the second tick to advance,
        # but the key assertion is that tick() itself runs to completion
        # a second time without raising -- proving the scheduler survived.
        second = sched.tick()
        assert "error" not in second or second.get("recovered") is not True
        assert sched.get_status()["cycles_executed"] == 2

    def test_recovered_exception_emits_a_scheduler_tick_recovered_event(self, agent_db, memory_store, monkeypatch):
        from agents import event_bus
        from agents.runtime import workflow_engine as wf

        rs.create_workflow(
            workflow_type="promotion", symbol="NIFTY", first_stage=wf.STAGE_MARKET_DATA, state={"symbol": "NIFTY"},
        )
        monkeypatch.setattr(wf, "advance", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("boom")))

        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        sched.tick()

        events = event_bus.events_since("2020-01-01", event_type="scheduler_tick_recovered")
        assert len(events) == 1

    def test_emergency_stop_short_circuit_still_updates_cycle_metrics(self, agent_db, memory_store):
        from agents.runtime import policy_engine as pe

        pe.set_policy("emergency_stop", changed_by="t", reason="halt")
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        result = sched.tick()
        assert result == {"emergency_stop": True}  # unchanged return-value contract
        assert sched.get_status()["cycles_executed"] == 1  # but metrics still tracked


class TestRunForeverInstallSignalHandlersFlag:
    def test_defaults_to_true_and_run_for_is_unaffected(self, agent_db, memory_store):
        """Regression guard: the new keyword-only param must not change
        run_for()'s or tick()'s own pre-existing behavior/signature."""
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store, tick_interval_seconds=0)
        results = sched.run_for(iterations=2, sleep_seconds=0)
        assert len(results) == 2


class TestSingletonLock:
    def test_second_acquisition_fails_while_first_is_held(self, tmp_path):
        path = str(tmp_path / "test.lock")
        fh1 = lifecycle._acquire_singleton_lock(path)
        try:
            assert fh1 is not None
            fh2 = lifecycle._acquire_singleton_lock(path)
            assert fh2 is None
        finally:
            lifecycle._release_singleton_lock(fh1)

    def test_lock_can_be_reacquired_after_release(self, tmp_path):
        path = str(tmp_path / "test.lock")
        fh1 = lifecycle._acquire_singleton_lock(path)
        lifecycle._release_singleton_lock(fh1)
        fh2 = lifecycle._acquire_singleton_lock(path)
        assert fh2 is not None
        lifecycle._release_singleton_lock(fh2)

    def test_release_is_safe_on_none(self):
        lifecycle._release_singleton_lock(None)  # must not raise


@pytest.fixture()
def _reset_lifecycle_globals():
    """agents.runtime.lifecycle holds module-level singleton state
    (_scheduler/_lock_file) -- must be reset around every test in this
    class so tests don't leak a real scheduler/lock into each other."""
    saved_scheduler, saved_lock = lifecycle._scheduler, lifecycle._lock_file
    lifecycle._scheduler, lifecycle._lock_file = None, None
    yield
    if lifecycle._scheduler is not None:
        lifecycle._scheduler.stop()
    if lifecycle._lock_file is not None:
        lifecycle._release_singleton_lock(lifecycle._lock_file)
    lifecycle._scheduler, lifecycle._lock_file = saved_scheduler, saved_lock


class TestStartSchedulerBackground:
    def test_disabled_by_default_is_a_no_op(self, agent_db, memory_store, monkeypatch, _reset_lifecycle_globals):
        monkeypatch.setattr(lifecycle.config, "RUNTIME_SCHEDULER_ENABLED", False)
        started = lifecycle.start_scheduler_background()
        assert started is False
        assert lifecycle.get_runtime_status()["scheduler_state"] == "stopped"

    def test_enabled_starts_via_the_injected_task_starter(
        self, agent_db, memory_store, monkeypatch, tmp_path, _reset_lifecycle_globals,
    ):
        """Wiring-only: the fake starter records the call but never
        actually invokes run_forever() (which blocks until stop()), so
        scheduler.start() itself hasn't run yet here -- that real,
        end-to-end behavior (state actually becoming "running") is
        covered by TestFullLoopIntegration's real background thread."""
        monkeypatch.setattr(lifecycle.config, "RUNTIME_SCHEDULER_ENABLED", True)
        monkeypatch.setattr(lifecycle.config, "RUNTIME_SCHEDULER_LOCK_PATH", str(tmp_path / "sched.lock"))
        calls = []

        def fake_starter(func, *args, **kwargs):
            calls.append((func, args, kwargs))  # never actually runs it -- no real thread/loop in this test

        started = lifecycle.start_scheduler_background(task_starter=fake_starter)
        assert started is True
        assert len(calls) == 1
        assert calls[0][0].__func__ is RuntimeScheduler.run_forever
        assert calls[0][2] == {"install_signal_handlers": False}
        assert lifecycle._scheduler is not None

    def test_a_second_call_in_the_same_process_is_a_no_op(
        self, agent_db, memory_store, monkeypatch, tmp_path, _reset_lifecycle_globals,
    ):
        monkeypatch.setattr(lifecycle.config, "RUNTIME_SCHEDULER_ENABLED", True)
        monkeypatch.setattr(lifecycle.config, "RUNTIME_SCHEDULER_LOCK_PATH", str(tmp_path / "sched.lock"))
        fake_starter = lambda func, *a, **kw: None

        first = lifecycle.start_scheduler_background(task_starter=fake_starter)
        second = lifecycle.start_scheduler_background(task_starter=fake_starter)
        assert first is True
        assert second is False

    def test_a_lost_lock_race_is_a_graceful_no_op(
        self, agent_db, memory_store, monkeypatch, tmp_path, _reset_lifecycle_globals,
    ):
        """Simulates a second OS process losing the singleton-lock race
        (e.g. a second gunicorn worker) -- must degrade honestly, never
        raise or silently start a duplicate scheduler."""
        lock_path = str(tmp_path / "sched.lock")
        monkeypatch.setattr(lifecycle.config, "RUNTIME_SCHEDULER_ENABLED", True)
        monkeypatch.setattr(lifecycle.config, "RUNTIME_SCHEDULER_LOCK_PATH", lock_path)

        other_process_fh = lifecycle._acquire_singleton_lock(lock_path)  # simulates a different process holding it
        try:
            started = lifecycle.start_scheduler_background(task_starter=lambda f, *a, **kw: None)
            assert started is False
            assert lifecycle.get_runtime_status()["scheduler_state"] == "stopped"
        finally:
            lifecycle._release_singleton_lock(other_process_fh)


class TestRuntimeStatusActiveJobs:
    def test_active_jobs_reflects_currently_running_agents(self, agent_db, memory_store):
        from agents.sys_admin import sysadmin_store

        sysadmin_store.set_currently_running("sys_admin", True)
        sysadmin_store.set_currently_running("memory", True)
        status = lifecycle.get_runtime_status()
        assert status["active_jobs"] == 2

    def test_active_jobs_is_zero_when_nothing_is_running(self, agent_db, memory_store):
        assert lifecycle.get_runtime_status()["active_jobs"] == 0


class TestFullLoopIntegration:
    """The one real threading-based integration test in this file: a
    genuine background thread runs RuntimeScheduler.run_forever() for a
    short, real window against a fully isolated throwaway database (ti_db
    covers agents.trading_intelligence's own DB_PATH too, so even the
    trading_intelligence cycle -- confirmed broker-isolated by Milestone
    12's own planning survey -- is safe to let run for real here)."""

    def test_scheduler_runs_repeated_real_cycles_then_shuts_down_gracefully(
        self, agent_db, ti_db, memory_store, monkeypatch,
    ):
        # quant_researcher's real cycle runs an actual backtest sweep over
        # RUNTIME_RESEARCH_SYMBOLS -- genuinely slow (multi-second) against
        # real archived candle files. Emptying the symbol list keeps this
        # an honest "no symbols configured" cycle (not a fabricated
        # stub of the cycle itself) so the test's own timing budget
        # reflects the scheduler loop, not one agent's real workload.
        monkeypatch.setattr(lifecycle.config, "RUNTIME_RESEARCH_SYMBOLS", ())

        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store, tick_interval_seconds=0.05)
        thread = threading.Thread(target=sched.run_forever, kwargs={"install_signal_handlers": False}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 30.0
            while sched.get_status()["cycles_executed"] < 2 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert sched.get_status()["cycles_executed"] >= 2, "scheduler did not execute repeated real cycles in time"
            assert sched.get_status()["scheduler_state"] == "running"
        finally:
            sched.stop()
            thread.join(timeout=10.0)
        assert not thread.is_alive(), "scheduler thread did not shut down gracefully within the timeout"
        assert sched.get_status()["scheduler_state"] == "stopped"

    def test_run_forever_emits_periodic_heartbeat_log_lines(
        self, agent_db, ti_db, memory_store, monkeypatch, caplog,
    ):
        monkeypatch.setattr(lifecycle.config, "RUNTIME_RESEARCH_SYMBOLS", ())
        monkeypatch.setattr(scheduler_module, "HEARTBEAT_LOG_EVERY_N_CYCLES", 2)  # fast, not the real 12

        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store, tick_interval_seconds=0.02)
        with caplog.at_level(logging.INFO, logger="oi_dashboard.runtime.scheduler"):
            thread = threading.Thread(
                target=sched.run_forever, kwargs={"install_signal_handlers": False}, daemon=True,
            )
            thread.start()
            try:
                deadline = time.monotonic() + 15.0
                while sched.get_status()["cycles_executed"] < 4 and time.monotonic() < deadline:
                    time.sleep(0.05)
            finally:
                sched.stop()
                thread.join(timeout=10.0)

        heartbeat_lines = [r for r in caplog.records if "scheduler heartbeat" in r.message]
        assert len(heartbeat_lines) >= 1, "no scheduler heartbeat log line was emitted"
        assert any("cycles executed" in r.message for r in heartbeat_lines)
