
from agents.runtime import policy_engine as pe
from agents.runtime import runtime_store as rs
from agents.runtime.scheduler import RuntimeScheduler


class TestStartAndStop:
    def test_start_records_a_report_and_emits_an_event(self, agent_db, memory_store):
        from agents import event_bus
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        sched.start()
        events = event_bus.events_since("2020-01-01", event_type="scheduler_started")
        assert len(events) == 1

    def test_start_resumes_running_workflows(self, agent_db, memory_store):
        from agents.runtime import workflow_engine as wf
        wid = rs.create_workflow(
            workflow_type="promotion", symbol="NIFTY", first_stage=wf.STAGE_EXECUTION,
            state={"symbol": "NIFTY", "decision": "APPROVED"},
        )
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        sched.start()
        # A running workflow at STAGE_EXECUTION must have been advanced
        # at least one stage forward by the resume sweep.
        w = rs.get_workflow(wid)
        assert w["current_stage"] != wf.STAGE_EXECUTION or w["status"] != "running"

    def test_stop_emits_scheduler_stopped(self, agent_db, memory_store):
        from agents import event_bus
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        sched.start()
        sched.stop()
        events = event_bus.events_since("2020-01-01", event_type="scheduler_stopped")
        assert len(events) == 1


class TestTick:
    def test_tick_returns_a_summary_dict(self, agent_db, memory_store):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        result = sched.tick()
        assert "agents_run" in result
        assert "queue_depth" in result

    def test_tick_respects_emergency_stop(self, agent_db, memory_store):
        pe.set_policy("emergency_stop", changed_by="t", reason="halt")
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        result = sched.tick()
        assert result == {"emergency_stop": True}

    def test_tick_only_runs_agents_whose_cadence_has_elapsed(self, agent_db, memory_store):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        first = sched.tick()
        agents_first = {a["agent"] for a in first["agents_run"]}
        assert len(agents_first) > 0  # never-run agents are always due

        second = sched.tick()  # immediately again -- nothing should be due yet
        agents_second = {a["agent"] for a in second["agents_run"]}
        assert agents_second == set()

    def test_market_session_gated_agents_are_skipped_outside_trading_hours(self, agent_db, memory_store, monkeypatch):
        from agents.runtime import market_session
        monkeypatch.setattr(market_session, "is_nse_session_open", lambda **kw: (False, "Weekend"))
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        result = sched.tick()
        agents_run = {a["agent"] for a in result["agents_run"]}
        assert "quant_researcher" not in agents_run
        assert "trading_supervisor" not in agents_run
        assert "sys_admin" in agents_run  # not gated by market hours

    def test_tick_advances_running_workflows(self, agent_db, memory_store):
        from agents.runtime import workflow_engine as wf
        wid = rs.create_workflow(workflow_type="promotion", symbol="NOPE_NOT_REAL", first_stage=wf.STAGE_MARKET_DATA, state={"symbol": "NOPE_NOT_REAL"})
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        result = sched.tick()
        advanced_ids = {w["workflow_id"] for w in result["workflows_advanced"]}
        assert wid in advanced_ids
        assert rs.get_workflow(wid)["status"] == "failed"  # no candle archive for this symbol


class TestRunFor:
    def test_run_for_executes_the_requested_number_of_ticks(self, agent_db, memory_store):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store, tick_interval_seconds=0)
        results = sched.run_for(iterations=3, sleep_seconds=0)
        assert len(results) == 3


class TestDueAgents:
    def test_never_run_agent_is_always_due(self, agent_db, memory_store):
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        due = sched._due_agents()
        assert "sys_admin" in due

    def test_recently_run_agent_is_not_due_again_immediately(self, agent_db, memory_store):
        from agents.sys_admin import sysadmin_store
        sysadmin_store.record_execution("sys_admin", success=True, duration_ms=1.0)
        sched = RuntimeScheduler(repo_dir=".", memory_store=memory_store)
        due = sched._due_agents()
        assert "sys_admin" not in due
