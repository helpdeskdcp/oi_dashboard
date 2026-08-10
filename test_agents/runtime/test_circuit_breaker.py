"""
test_agents/runtime/test_circuit_breaker.py -- Milestone 15, Phase 4:
Self-Healing Runtime Protection. Pure unit tests for
agents/runtime/circuit_breaker.py's own state machine (no DB, no
scheduler needed), plus integration tests confirming RuntimeScheduler's
own tick() actually wires it in -- reuses agent_db/memory_store from
test_agents/runtime/conftest.py, same as every other file in this
directory.
"""
import datetime as dt

import pytest

from agents.ops import event_log as ops_event_log, models as ops_models
from agents.runtime import circuit_breaker as cb
from agents.runtime.scheduler import RuntimeScheduler


@pytest.fixture(autouse=True)
def _ops_event_log_db(monkeypatch, tmp_path):
    """Autouse for this whole file -- circuit_breaker.py now calls
    ops_event_log.record_event_safe() on every state transition, and
    several tests below construct a CircuitBreaker directly without
    going through the agent_db fixture (which already isolates it).
    Without this, those tests would silently write into this
    worktree's real local oi_history.db (record_event_safe() degrades
    to a no-op on a missing table, but a PRESENT one -- e.g. from a
    real app.py run in this worktree -- would actually be written to)."""
    db_path = str(tmp_path / "ops_standalone.db")
    monkeypatch.setattr(ops_event_log, "DB_PATH", db_path)
    ops_event_log.init_db()
    return db_path


class TestCircuitBreakerStateMachine:
    def test_starts_closed(self):
        breaker = cb.CircuitBreaker(failure_threshold=3, recovery_seconds=60)
        assert breaker.state == cb.CLOSED
        assert breaker.should_allow_execution() is True

    def test_stays_closed_below_threshold(self):
        breaker = cb.CircuitBreaker(failure_threshold=3, recovery_seconds=60)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.state == cb.CLOSED
        assert breaker.should_allow_execution() is True

    def test_repeated_failures_open_the_circuit(self):
        breaker = cb.CircuitBreaker(failure_threshold=3, recovery_seconds=60)
        for _ in range(3):
            breaker.record_failure()
        assert breaker.state == cb.OPEN

    def test_open_circuit_skips_execution(self):
        breaker = cb.CircuitBreaker(failure_threshold=2, recovery_seconds=300)
        breaker.record_failure()
        breaker.record_failure()
        assert breaker.should_allow_execution() is False

    def test_open_circuit_stays_open_before_recovery_window_elapses(self):
        breaker = cb.CircuitBreaker(failure_threshold=2, recovery_seconds=300)
        breaker.record_failure()
        breaker.record_failure()
        soon = dt.datetime.now() + dt.timedelta(seconds=100)
        assert breaker.should_allow_execution(now=soon) is False
        assert breaker.state == cb.OPEN

    def test_half_open_allows_a_probe_run_after_recovery_window(self):
        breaker = cb.CircuitBreaker(failure_threshold=2, recovery_seconds=60)
        breaker.record_failure()
        breaker.record_failure()
        later = dt.datetime.now() + dt.timedelta(seconds=61)
        assert breaker.should_allow_execution(now=later) is True
        assert breaker.state == cb.HALF_OPEN

    def test_successful_probe_closes_the_circuit(self):
        breaker = cb.CircuitBreaker(failure_threshold=2, recovery_seconds=60)
        breaker.record_failure()
        breaker.record_failure()
        later = dt.datetime.now() + dt.timedelta(seconds=61)
        breaker.should_allow_execution(now=later)  # -> half_open
        breaker.record_success()
        assert breaker.state == cb.CLOSED
        assert breaker.consecutive_failures == 0

    def test_failed_probe_reopens_the_circuit_and_resets_the_timer(self):
        breaker = cb.CircuitBreaker(failure_threshold=2, recovery_seconds=60)
        breaker.record_failure()
        breaker.record_failure()
        probe_time = dt.datetime.now() + dt.timedelta(seconds=61)
        breaker.should_allow_execution(now=probe_time)  # -> half_open
        breaker.record_failure(now=probe_time)
        assert breaker.state == cb.OPEN
        # must wait a FRESH recovery_seconds from the failed probe, not
        # from the original open -- confirm it's still closed shortly after
        soon_after_probe = probe_time + dt.timedelta(seconds=10)
        assert breaker.should_allow_execution(now=soon_after_probe) is False

    def test_success_while_closed_keeps_it_closed_and_resets_counter(self):
        breaker = cb.CircuitBreaker(failure_threshold=3, recovery_seconds=60)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        assert breaker.state == cb.CLOSED
        assert breaker.consecutive_failures == 0

    def test_to_dict_shape(self):
        breaker = cb.CircuitBreaker(failure_threshold=3, recovery_seconds=60)
        assert breaker.to_dict() == {"circuit_state": "closed", "circuit_consecutive_failures": 0}


class TestCircuitBreakerLogging:
    def test_opening_is_logged(self, caplog):
        breaker = cb.CircuitBreaker(failure_threshold=1, recovery_seconds=60)
        with caplog.at_level("INFO", logger="oi_dashboard.runtime.circuit_breaker"):
            breaker.record_failure()
        assert "RUNTIME_CIRCUIT_OPENED" in caplog.text

    def test_half_open_transition_is_logged(self, caplog):
        breaker = cb.CircuitBreaker(failure_threshold=1, recovery_seconds=60)
        breaker.record_failure()
        later = dt.datetime.now() + dt.timedelta(seconds=61)
        with caplog.at_level("INFO", logger="oi_dashboard.runtime.circuit_breaker"):
            breaker.should_allow_execution(now=later)
        assert "RUNTIME_CIRCUIT_HALF_OPEN" in caplog.text

    def test_closing_is_logged(self, caplog):
        breaker = cb.CircuitBreaker(failure_threshold=1, recovery_seconds=60)
        breaker.record_failure()
        later = dt.datetime.now() + dt.timedelta(seconds=61)
        breaker.should_allow_execution(now=later)
        with caplog.at_level("INFO", logger="oi_dashboard.runtime.circuit_breaker"):
            breaker.record_success()
        assert "RUNTIME_CIRCUIT_CLOSED" in caplog.text


class TestSchedulerCircuitBreakerIntegration:
    def test_default_config(self):
        from agents import config
        assert config.RUNTIME_CIRCUIT_FAILURE_THRESHOLD == 5
        assert config.RUNTIME_CIRCUIT_RECOVERY_SECONDS == 300

    def test_get_status_includes_circuit_fields(self, agent_db, memory_store):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        sched.tick()
        status = sched.get_status()
        assert status["circuit_state"] == "closed"
        assert status["circuit_consecutive_failures"] == 0

    def test_repeated_scheduler_failures_open_the_circuit(self, agent_db, memory_store, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "RUNTIME_CIRCUIT_FAILURE_THRESHOLD", 2)
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)

        def _boom():
            raise RuntimeError("simulated scheduler-level failure")
        monkeypatch.setattr(sched, "_due_agents", _boom)

        sched.tick()
        sched.tick()
        assert sched.get_status()["circuit_state"] == "open"

    def test_open_circuit_makes_tick_skip_execution(self, agent_db, memory_store, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "RUNTIME_CIRCUIT_FAILURE_THRESHOLD", 1)
        monkeypatch.setattr(config, "RUNTIME_CIRCUIT_RECOVERY_SECONDS", 300)
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)

        def _boom():
            raise RuntimeError("simulated")
        monkeypatch.setattr(sched, "_due_agents", _boom)
        sched.tick()  # opens the circuit
        assert sched.get_status()["circuit_state"] == "open"

        # fix the underlying problem -- circuit should still skip (too soon)
        monkeypatch.setattr(sched, "_due_agents", lambda: [])
        result = sched.tick()
        assert result == {"circuit_open": True, "circuit_state": "open"}

    def test_half_open_probe_run_succeeds_and_closes_circuit(self, agent_db, memory_store, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "RUNTIME_CIRCUIT_FAILURE_THRESHOLD", 1)
        monkeypatch.setattr(config, "RUNTIME_CIRCUIT_RECOVERY_SECONDS", 60)
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)

        def _boom():
            raise RuntimeError("simulated")
        monkeypatch.setattr(sched, "_due_agents", _boom)
        sched.tick()
        assert sched.get_status()["circuit_state"] == "open"

        monkeypatch.setattr(sched, "_due_agents", lambda: [])  # fix the underlying problem
        sched._circuit_breaker._opened_at = dt.datetime.now() - dt.timedelta(seconds=61)  # simulate elapsed recovery
        result = sched.tick()
        assert "circuit_open" not in result  # real work ran (the probe)
        assert sched.get_status()["circuit_state"] == "closed"

    def test_emergency_stop_does_not_count_as_a_circuit_failure(self, agent_db, memory_store):
        """emergency_stop is a deliberate operator pause, not a scheduler
        malfunction -- it must never open the circuit, and must never
        reset an already-accumulating failure count either."""
        from agents.runtime import policy_engine as pe
        pe.set_policy("emergency_stop", changed_by="t", reason="halt")
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        sched.tick()
        sched.tick()
        status = sched.get_status()
        assert status["circuit_state"] == "closed"
        assert status["circuit_consecutive_failures"] == 0

    def test_uninitialized_scheduler_status_includes_circuit_fields_as_honest_defaults(self):
        from agents.runtime import lifecycle
        with lifecycle._state_lock:
            saved = lifecycle._scheduler
            lifecycle._scheduler = None
        try:
            status = lifecycle.get_runtime_status()
        finally:
            with lifecycle._state_lock:
                lifecycle._scheduler = saved
        assert status["circuit_state"] == "closed"
        assert status["circuit_consecutive_failures"] == 0


# --- Milestone 16, Phase 1: ops event log wiring ---------------------------------

class TestCircuitBreakerOpsEventWiring:
    def test_opening_emits_circuit_opened_event(self, _ops_event_log_db):
        breaker = cb.CircuitBreaker(failure_threshold=1, recovery_seconds=60)
        breaker.record_failure()
        events = ops_event_log.get_events(event_type=ops_models.CIRCUIT_OPENED)
        assert len(events) == 1
        assert events[0]["payload"]["probe_failed"] is False

    def test_half_open_emits_circuit_half_open_event(self, _ops_event_log_db):
        breaker = cb.CircuitBreaker(failure_threshold=1, recovery_seconds=60)
        breaker.record_failure()
        later = dt.datetime.now() + dt.timedelta(seconds=61)
        breaker.should_allow_execution(now=later)
        events = ops_event_log.get_events(event_type=ops_models.CIRCUIT_HALF_OPEN)
        assert len(events) == 1

    def test_closing_emits_circuit_closed_event(self, _ops_event_log_db):
        breaker = cb.CircuitBreaker(failure_threshold=1, recovery_seconds=60)
        breaker.record_failure()
        later = dt.datetime.now() + dt.timedelta(seconds=61)
        breaker.should_allow_execution(now=later)
        breaker.record_success()
        events = ops_event_log.get_events(event_type=ops_models.CIRCUIT_CLOSED)
        assert len(events) == 1

    def test_staying_closed_on_success_does_not_emit_an_event(self, _ops_event_log_db):
        breaker = cb.CircuitBreaker(failure_threshold=3, recovery_seconds=60)
        breaker.record_success()
        assert ops_event_log.count_events(event_type=ops_models.CIRCUIT_CLOSED) == 0

    def test_scheduler_start_stop_emit_ops_events(self, agent_db, memory_store):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        sched.start()
        sched.stop()
        assert ops_event_log.count_events(event_type=ops_models.SCHEDULER_STARTED) == 1
        assert ops_event_log.count_events(event_type=ops_models.SCHEDULER_STOPPED) == 1

    def test_scheduler_emits_heartbeat_updated_at_the_configured_cadence(self, agent_db, memory_store):
        from agents.runtime.scheduler import HEARTBEAT_LOG_EVERY_N_CYCLES
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        for _ in range(HEARTBEAT_LOG_EVERY_N_CYCLES - 1):
            sched.tick()
        assert ops_event_log.count_events(event_type=ops_models.HEARTBEAT_UPDATED) == 0
        sched.tick()  # the Nth tick
        assert ops_event_log.count_events(event_type=ops_models.HEARTBEAT_UPDATED) == 1
