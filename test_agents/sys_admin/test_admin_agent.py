"""
test_agents/sys_admin/test_admin_agent.py -- regression tests for
SystemAdministrator(BaseAgent).
"""
from agents import event_bus, registry
from agents.memory.sqlite_store import SQLiteMemoryStore
from agents.sys_admin import admin_agent, orchestrator, sysadmin_store
from agents.sys_admin.admin_agent import SystemAdministrator


class TestRegistration:
    def test_registered_under_sys_admin(self):
        assert registry.get_agent("sys_admin") is SystemAdministrator

    def test_is_a_base_agent(self):
        from agents.base_agent import BaseAgent
        assert issubclass(SystemAdministrator, BaseAgent)


class TestRunCycle:
    def _admin(self, tmp_path, agent_db, **kwargs):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        return SystemAdministrator(
            memory_store=store, source_paths=[], db_path=str(tmp_path / "missing.db"), repo_dir=".", **kwargs
        )

    def test_crashed_agent_is_auto_healed_and_reported(self, agent_db, tmp_path):
        orchestrator.record_crash("risk_manager", reason="boom")
        admin = self._admin(tmp_path, agent_db)

        findings = admin.run_cycle()

        status = sysadmin_store.get_agent_status("risk_manager")
        assert status["crashed"] == 0  # auto-healed
        assert any("automatically cleared" in f.summary for f in findings)

    def test_unhealthy_memory_dependency_is_a_critical_finding(self, agent_db):
        class BrokenStore:
            def search_bug_fixes(self, *a, **k):
                raise RuntimeError("gone")

        admin = SystemAdministrator(memory_store=BrokenStore(), source_paths=[], db_path="does-not-exist.db")
        findings = admin.run_cycle()
        assert any(f.severity == "critical" and "unhealthy" in f.summary for f in findings)

    def test_critical_findings_are_published_to_event_bus(self, agent_db):
        class BrokenStore:
            def search_bug_fixes(self, *a, **k):
                raise RuntimeError("gone")

        admin = SystemAdministrator(memory_store=BrokenStore(), source_paths=[], db_path="does-not-exist.db")
        admin.run_cycle()
        events = event_bus.events_since("2000-01-01")
        assert any(e["event_type"] == "sysadmin_alert" for e in events)

    def test_clean_state_produces_no_orchestration_findings(self, agent_db, tmp_path):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        admin = SystemAdministrator(memory_store=store, source_paths=[], db_path=str(tmp_path / "t.db"))
        # missing DB will still produce an infra/security finding, but no
        # orchestration-level (crash/dependency) finding should appear.
        findings = admin._orchestration_findings()
        assert findings == []


class TestOnEvent:
    def test_critical_risk_alert_is_recorded(self, agent_db):
        admin = SystemAdministrator()
        admin.on_event({
            "event_type": "risk_alert", "severity": "critical", "source_agent": "risk_manager",
            "payload_json": {"metric": "portfolio_heat"},
        })
        reports = sysadmin_store.list_reports(module="admin_agent")
        assert len(reports) == 1
        assert reports[0]["report_json"]["action"] == "cross_agent_escalation"

    def test_non_critical_event_is_ignored(self, agent_db):
        admin = SystemAdministrator()
        admin.on_event({"event_type": "risk_alert", "severity": "warning", "payload_json": {}})
        assert sysadmin_store.list_reports(module="admin_agent") == []

    def test_unrelated_event_type_is_ignored(self, agent_db):
        admin = SystemAdministrator()
        admin.on_event({"event_type": "something_else", "severity": "critical", "payload_json": {}})
        assert sysadmin_store.list_reports(module="admin_agent") == []
