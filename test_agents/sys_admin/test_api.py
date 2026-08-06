"""
test_agents/sys_admin/test_api.py -- regression tests for the
Operations Dashboard's read functions.
"""
from agents.memory.sqlite_store import SQLiteMemoryStore
from agents.sys_admin import api, orchestrator, sysadmin_store


class TestGetOverview:
    def test_returns_every_documented_section(self, agent_db, tmp_path):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        overview = api.get_overview(memory_store=store, db_path=str(tmp_path / "missing.db"))
        assert set(overview.keys()) == {
            "agents", "infrastructure", "risk_state", "supervision_state",
            "backup_state", "security_alerts", "recovery_history", "recent_findings",
        }
        assert set(overview["agents"].keys()) == set(orchestrator.AGENT_NAMES)


class TestGetAgentStatus:
    def test_reflects_orchestrator_state(self, agent_db):
        orchestrator.disable_agent("dev_agent", reason="maintenance")
        statuses = api.get_agent_status()
        dev = next(s for s in statuses if s["agent"] == "dev_agent")
        assert dev["enabled"] == 0


class TestGetRecentFindings:
    def test_delegates_to_sysadmin_store(self, agent_db):
        from agents.sys_admin import sysadmin_report
        sysadmin_store.record_report(sysadmin_report.build(
            module="maintenance", action="a", reason="r", confidence=1, evidence={}, severity="warning",
        ))
        hits = api.get_recent_findings(module="maintenance")
        assert len(hits) == 1


class TestGetBackups:
    def test_delegates_to_sysadmin_store(self, agent_db):
        sysadmin_store.record_backup(
            backup_path="/tmp/b.db", source_db_path="x.db", size_bytes=1, verified=True, integrity_ok=True,
        )
        assert len(api.get_backups(verified_only=True)) == 1
