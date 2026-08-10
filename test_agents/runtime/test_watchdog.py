"""
test_agents/runtime/test_watchdog.py -- Milestone 16, Phase 3: Watchdog
& Stale-Cycle Detection. Pure unit tests for agents/runtime/watchdog.py's
own Watchdog class (no DB, no scheduler needed -- ops_event_log's
record_event_safe() degrades harmlessly without init_db()), plus
integration tests confirming RuntimeScheduler's own tick() wires it in.
"""
import datetime as dt

from agents.runtime.scheduler import RuntimeScheduler
from agents.runtime.watchdog import Watchdog


class TestWatchdogHealthySchedulerNeverStale:
    def test_recent_success_is_not_stale(self):
        wd = Watchdog(enabled=True, stale_multiplier=3)
        now = dt.datetime.now()
        result = wd.check(last_successful_cycle=now, cycle_interval_seconds=5, now=now)
        assert result == {"stale": False, "restart_recommended": False}

    def test_success_just_under_the_threshold_is_not_stale(self):
        wd = Watchdog(enabled=True, stale_multiplier=3)
        now = dt.datetime.now()
        just_under = now + dt.timedelta(seconds=14.9)  # threshold is 5*3=15s
        result = wd.check(last_successful_cycle=now, cycle_interval_seconds=5, now=just_under)
        assert result["stale"] is False

    def test_never_succeeded_yet_is_not_stale(self):
        """Honest "don't know yet," not a fabricated stale trigger from
        insufficient data -- same discipline as every check_*() in
        agents/intelligence_alerts/rules.py."""
        wd = Watchdog(enabled=True, stale_multiplier=3)
        result = wd.check(last_successful_cycle=None, cycle_interval_seconds=5, now=dt.datetime.now())
        assert result == {"stale": False, "restart_recommended": False}

    def test_disabled_watchdog_never_reports_stale(self):
        wd = Watchdog(enabled=False, stale_multiplier=3)
        now = dt.datetime.now()
        long_ago = now - dt.timedelta(hours=1)
        result = wd.check(last_successful_cycle=long_ago, cycle_interval_seconds=5, now=now)
        assert result == {"stale": False, "restart_recommended": False}


class TestWatchdogStaleDetection:
    def test_success_beyond_threshold_is_stale(self):
        wd = Watchdog(enabled=True, stale_multiplier=3)
        now = dt.datetime.now()
        stale_check = now + dt.timedelta(seconds=20)  # threshold 15s
        result = wd.check(last_successful_cycle=now, cycle_interval_seconds=5, now=stale_check)
        assert result == {"stale": True, "restart_recommended": True}

    def test_stale_count_increments_on_first_detection(self):
        wd = Watchdog(enabled=True, stale_multiplier=3)
        now = dt.datetime.now()
        wd.check(last_successful_cycle=now, cycle_interval_seconds=5, now=now + dt.timedelta(seconds=20))
        assert wd.stale_count == 1

    def test_custom_stale_multiplier(self):
        wd = Watchdog(enabled=True, stale_multiplier=2)
        now = dt.datetime.now()
        # threshold is 5*2=10s -- 12s elapsed should be stale
        result = wd.check(last_successful_cycle=now, cycle_interval_seconds=5, now=now + dt.timedelta(seconds=12))
        assert result["stale"] is True


class TestWatchdogStaleEventDeduplication:
    def test_repeated_checks_while_still_stale_do_not_increment_stale_count(self):
        wd = Watchdog(enabled=True, stale_multiplier=3)
        now = dt.datetime.now()
        wd.check(last_successful_cycle=now, cycle_interval_seconds=5, now=now + dt.timedelta(seconds=20))
        wd.check(last_successful_cycle=now, cycle_interval_seconds=5, now=now + dt.timedelta(seconds=25))
        wd.check(last_successful_cycle=now, cycle_interval_seconds=5, now=now + dt.timedelta(seconds=30))
        assert wd.stale_count == 1

    def test_a_second_genuine_stale_episode_after_recovery_increments_again(self):
        """Deduplication only suppresses repeats WITHIN one continuous
        stale episode -- a second, separate episode (after a real
        recovery in between) is a genuinely new event."""
        wd = Watchdog(enabled=True, stale_multiplier=3)
        t0 = dt.datetime.now()
        wd.check(last_successful_cycle=t0, cycle_interval_seconds=5, now=t0 + dt.timedelta(seconds=20))
        assert wd.stale_count == 1

        t1 = t0 + dt.timedelta(seconds=30)  # a fresh success -- recovers
        wd.check(last_successful_cycle=t1, cycle_interval_seconds=5, now=t1)
        assert wd.is_stale is False

        wd.check(last_successful_cycle=t1, cycle_interval_seconds=5, now=t1 + dt.timedelta(seconds=20))
        assert wd.stale_count == 2


class TestWatchdogStaleRecovery:
    def test_a_fresh_success_clears_staleness(self):
        wd = Watchdog(enabled=True, stale_multiplier=3)
        t0 = dt.datetime.now()
        wd.check(last_successful_cycle=t0, cycle_interval_seconds=5, now=t0 + dt.timedelta(seconds=20))
        assert wd.is_stale is True

        t1 = t0 + dt.timedelta(seconds=25)
        result = wd.check(last_successful_cycle=t1, cycle_interval_seconds=5, now=t1)
        assert result == {"stale": False, "restart_recommended": False}
        assert wd.is_stale is False

    def test_to_dict_shape(self):
        wd = Watchdog(enabled=True, stale_multiplier=3)
        assert wd.to_dict() == {
            "watchdog_stale": False, "watchdog_restart_recommended": False, "watchdog_stale_count": 0,
        }


class TestWatchdogLogging:
    def test_stale_detection_is_logged(self, caplog):
        wd = Watchdog(enabled=True, stale_multiplier=3)
        now = dt.datetime.now()
        with caplog.at_level("WARNING", logger="oi_dashboard.runtime.watchdog"):
            wd.check(last_successful_cycle=now, cycle_interval_seconds=5, now=now + dt.timedelta(seconds=20))
        assert "WATCHDOG_STALE_CYCLE" in caplog.text


class TestWatchdogOpsEventWiring:
    def test_stale_detection_emits_watchdog_stale_cycle_event(self, monkeypatch, tmp_path):
        from agents.ops import event_log as ops_event_log, models as ops_models
        db_path = str(tmp_path / "ops.db")
        monkeypatch.setattr(ops_event_log, "DB_PATH", db_path)
        ops_event_log.init_db()

        wd = Watchdog(enabled=True, stale_multiplier=3)
        now = dt.datetime.now()
        wd.check(last_successful_cycle=now, cycle_interval_seconds=5, now=now + dt.timedelta(seconds=20))

        events = ops_event_log.get_events(event_type=ops_models.WATCHDOG_STALE_CYCLE)
        assert len(events) == 1
        assert events[0]["payload"]["stale_count"] == 1

    def test_repeated_stale_checks_emit_only_one_event(self, monkeypatch, tmp_path):
        from agents.ops import event_log as ops_event_log, models as ops_models
        db_path = str(tmp_path / "ops.db")
        monkeypatch.setattr(ops_event_log, "DB_PATH", db_path)
        ops_event_log.init_db()

        wd = Watchdog(enabled=True, stale_multiplier=3)
        now = dt.datetime.now()
        wd.check(last_successful_cycle=now, cycle_interval_seconds=5, now=now + dt.timedelta(seconds=20))
        wd.check(last_successful_cycle=now, cycle_interval_seconds=5, now=now + dt.timedelta(seconds=25))
        assert ops_event_log.count_events(event_type=ops_models.WATCHDOG_STALE_CYCLE) == 1


class TestSchedulerWatchdogIntegration:
    def test_default_config(self):
        from agents import config
        assert config.WATCHDOG_ENABLED is True
        assert config.WATCHDOG_STALE_MULTIPLIER == 3

    def test_get_status_includes_watchdog_fields(self, agent_db, memory_store):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        sched.tick()
        status = sched.get_status()
        for key in ("watchdog_stale", "watchdog_restart_recommended", "watchdog_stale_count"):
            assert key in status
        assert status["watchdog_stale"] is False

    def test_disabled_via_config_never_reports_stale(self, agent_db, memory_store, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "WATCHDOG_ENABLED", False)  # must be set BEFORE construction --
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store, tick_interval_seconds=0.01)  # __init__ reads it once
        sched.tick()
        # simulate a long gap by directly manipulating the heartbeat's own timestamp
        sched._heartbeat.last_successful_cycle = dt.datetime.now() - dt.timedelta(hours=1)
        sched.tick()
        assert sched.get_status()["watchdog_stale"] is False

    def test_uninitialized_scheduler_status_includes_watchdog_fields_as_honest_defaults(self):
        from agents.runtime import lifecycle
        with lifecycle._state_lock:
            saved = lifecycle._scheduler
            lifecycle._scheduler = None
        try:
            status = lifecycle.get_runtime_status()
        finally:
            with lifecycle._state_lock:
                lifecycle._scheduler = saved
        assert status["watchdog_stale"] is False
        assert status["watchdog_restart_recommended"] is False
        assert status["watchdog_stale_count"] == 0
