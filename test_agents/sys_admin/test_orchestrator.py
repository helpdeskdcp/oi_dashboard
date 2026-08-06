"""
test_agents/sys_admin/test_orchestrator.py -- regression tests for
orchestrator.py.
"""
from agents.memory.sqlite_store import SQLiteMemoryStore
from agents.sys_admin import orchestrator, sysadmin_store


class TestEnableDisable:
    def test_unknown_agent_defaults_to_enabled(self, agent_db):
        assert orchestrator.is_enabled("dev_agent") is True

    def test_disable_then_enable(self, agent_db):
        orchestrator.disable_agent("dev_agent", reason="maintenance")
        assert orchestrator.is_enabled("dev_agent") is False
        orchestrator.enable_agent("dev_agent")
        assert orchestrator.is_enabled("dev_agent") is True

    def test_disable_records_a_warning_report(self, agent_db):
        orchestrator.disable_agent("dev_agent", reason="maintenance")
        reports = sysadmin_store.list_reports(module="orchestrator")
        assert any(r["severity"] == "warning" and r["report_json"]["action"] == "disable_agent" for r in reports)


class TestHeartbeatAndCrash:
    def test_heartbeat_sets_last_heartbeat_ts(self, agent_db):
        orchestrator.heartbeat("quant_researcher")
        status = sysadmin_store.get_agent_status("quant_researcher")
        assert status["last_heartbeat_ts"] is not None

    def test_record_crash_marks_crashed_and_logs_critical(self, agent_db):
        orchestrator.record_crash("risk_manager", reason="unhandled exception")
        status = sysadmin_store.get_agent_status("risk_manager")
        assert status["crashed"] == 1
        assert status["crash_reason"] == "unhandled exception"
        reports = sysadmin_store.list_reports(severity="critical")
        assert any(r["report_json"]["action"] == "record_crash" for r in reports)


class TestRestartAgent:
    def test_restart_clears_crashed_flag(self, agent_db):
        orchestrator.record_crash("risk_manager", reason="boom")
        report = orchestrator.restart_agent("risk_manager")
        status = sysadmin_store.get_agent_status("risk_manager")
        assert status["crashed"] == 0
        assert status["crash_reason"] is None
        assert report.recovery_outcome is not None

    def test_restart_on_a_never_crashed_agent_is_a_safe_noop(self, agent_db):
        report = orchestrator.restart_agent("dev_agent")
        assert "was not marked crashed" in report.reason


class TestDependencyHealth:
    def test_healthy_memory_reports_healthy(self, agent_db, tmp_path):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        health = orchestrator.dependency_health("quant_researcher", store)
        assert health["memory"]["healthy"] is True

    def test_unreachable_memory_reports_unhealthy(self, agent_db):
        class BrokenStore:
            def search_bug_fixes(self, *a, **k):
                raise RuntimeError("gone")

        health = orchestrator.dependency_health("quant_researcher", BrokenStore())
        assert health["memory"]["healthy"] is False

    def test_trading_supervisor_depends_on_four_things(self, agent_db, tmp_path):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        health = orchestrator.dependency_health("trading_supervisor", store)
        assert set(health.keys()) == {"memory", "dev_agent", "quant_researcher", "risk_manager"}


class TestRegistrySnapshot:
    def test_covers_every_known_agent(self, agent_db, tmp_path):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        snapshot = orchestrator.registry_snapshot(store)
        assert set(snapshot.keys()) == set(orchestrator.AGENT_NAMES)
        for agent, state in snapshot.items():
            assert "dependencies" in state
            assert state["enabled"] == 1  # default before any explicit disable
