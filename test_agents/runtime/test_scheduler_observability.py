"""
test_agents/runtime/test_scheduler_observability.py -- Milestone 15,
Phase 3: Runtime Scheduler Observability. Pure unit tests for
agents/runtime/heartbeat.py and agents/runtime/metrics.py (no DB, no
scheduler needed), plus integration tests confirming RuntimeScheduler's
own tick()/get_status() actually wire them in -- reuses agent_db/
memory_store from test_agents/runtime/conftest.py, same as every other
file in this directory.
"""
import datetime as dt

from agents.runtime import heartbeat, metrics
from agents.runtime import policy_engine as pe
from agents.runtime.scheduler import RuntimeScheduler


class TestCycleHeartbeat:
    def test_fresh_heartbeat_has_no_history(self):
        hb = heartbeat.CycleHeartbeat()
        assert hb.to_dict() == {
            "last_successful_cycle": None, "last_failed_cycle": None, "consecutive_failures": 0,
        }

    def test_record_success_sets_timestamp(self):
        hb = heartbeat.CycleHeartbeat()
        ts = dt.datetime(2026, 8, 10, 12, 0, 0)
        hb.record_success(ts)
        assert hb.to_dict()["last_successful_cycle"] == ts.isoformat()
        assert hb.to_dict()["last_failed_cycle"] is None

    def test_record_failure_increments_consecutive_failures(self):
        hb = heartbeat.CycleHeartbeat()
        now = dt.datetime.now()
        hb.record_failure(now)
        hb.record_failure(now)
        hb.record_failure(now)
        assert hb.consecutive_failures == 3
        assert hb.to_dict()["last_failed_cycle"] == now.isoformat()

    def test_success_resets_consecutive_failures_to_zero(self):
        hb = heartbeat.CycleHeartbeat()
        now = dt.datetime.now()
        hb.record_failure(now)
        hb.record_failure(now)
        hb.record_success(now)
        assert hb.consecutive_failures == 0

    def test_last_successful_and_failed_are_tracked_independently(self):
        hb = heartbeat.CycleHeartbeat()
        t1 = dt.datetime(2026, 8, 10, 9, 0, 0)
        t2 = dt.datetime(2026, 8, 10, 9, 5, 0)
        hb.record_success(t1)
        hb.record_failure(t2)
        d = hb.to_dict()
        assert d["last_successful_cycle"] == t1.isoformat()  # unchanged by the later failure
        assert d["last_failed_cycle"] == t2.isoformat()


class TestRunningAverage:
    def test_empty_average_is_none(self):
        assert metrics.RunningAverage().average is None

    def test_single_value_average(self):
        avg = metrics.RunningAverage()
        avg.add(100.0)
        assert avg.average == 100.0

    def test_average_of_multiple_values(self):
        avg = metrics.RunningAverage()
        for v in (100.0, 200.0, 300.0):
            avg.add(v)
        assert avg.average == 200.0

    def test_average_rounds_to_two_decimals(self):
        avg = metrics.RunningAverage()
        avg.add(1.0)
        avg.add(2.0)
        avg.add(2.0)
        assert avg.average == 1.67


class TestSchedulerObservabilityIntegration:
    def test_get_status_includes_new_observability_fields(self, agent_db, memory_store):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        sched.tick()
        status = sched.get_status()
        for key in ("average_cycle_duration_ms", "last_successful_cycle", "last_failed_cycle", "consecutive_failures"):
            assert key in status

    def test_successful_cycle_updates_last_successful_cycle(self, agent_db, memory_store):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        sched.tick()
        status = sched.get_status()
        assert status["last_successful_cycle"] is not None
        assert status["last_failed_cycle"] is None
        assert status["consecutive_failures"] == 0

    def test_metrics_survive_multiple_cycles(self, agent_db, memory_store):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        sched.tick()
        sched.tick()
        sched.tick()
        status = sched.get_status()
        assert status["cycles_executed"] == 3
        assert status["average_cycle_duration_ms"] is not None

    def test_emergency_stop_tick_still_counts_as_a_successful_cycle(self, agent_db, memory_store):
        """emergency_stop short-circuits tick() to {"emergency_stop": True}
        without raising -- that's not a scheduler FAILURE (nothing broke),
        so it must still record a successful heartbeat, not a failed one."""
        pe.set_policy("emergency_stop", changed_by="t", reason="halt")
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        sched.tick()
        status = sched.get_status()
        assert status["last_successful_cycle"] is not None
        assert status["consecutive_failures"] == 0

    def test_failure_counter_increments_correctly(self, agent_db, memory_store, monkeypatch):
        """A genuinely broken due-agents computation must be recorded as
        a heartbeat failure, and consecutive_failures must climb across
        repeated failing ticks."""
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)

        def _boom():
            raise RuntimeError("simulated scheduler-level failure")
        monkeypatch.setattr(sched, "_due_agents", _boom)

        sched.tick()
        status = sched.get_status()
        assert status["consecutive_failures"] == 1
        assert status["last_failed_cycle"] is not None

        sched.tick()
        assert sched.get_status()["consecutive_failures"] == 2

    def test_a_success_after_failures_resets_consecutive_failures(self, agent_db, memory_store, monkeypatch):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        real_due_agents = sched._due_agents

        def _boom():
            raise RuntimeError("simulated failure")
        monkeypatch.setattr(sched, "_due_agents", _boom)
        sched.tick()
        assert sched.get_status()["consecutive_failures"] == 1

        # monkeypatch.undo() would revert every patch made through this
        # shared, function-scoped monkeypatch instance -- including
        # agent_db's DB_PATH patches -- not just this one. Restore via a
        # second setattr instead so only _due_agents is touched.
        monkeypatch.setattr(sched, "_due_agents", real_due_agents)
        sched.tick()
        assert sched.get_status()["consecutive_failures"] == 0

    def test_uninitialized_scheduler_status_includes_new_fields_as_honest_defaults(self):
        """agents/runtime/lifecycle.py's own "no scheduler yet" fallback
        dict must carry the same new keys, honestly None/0 -- never
        omitted, which would look like a missing feature rather than an
        honest "nothing has run yet"."""
        from agents.runtime import lifecycle
        with lifecycle._state_lock:
            saved = lifecycle._scheduler
            lifecycle._scheduler = None
        try:
            status = lifecycle.get_runtime_status()
        finally:
            with lifecycle._state_lock:
                lifecycle._scheduler = saved
        assert status["average_cycle_duration_ms"] is None
        assert status["last_successful_cycle"] is None
        assert status["last_failed_cycle"] is None
        assert status["consecutive_failures"] == 0
