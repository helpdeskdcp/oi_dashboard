"""
test_agents/sys_admin/test_sysadmin_store.py -- regression tests for
sysadmin_store.py's SQLite persistence. Uses the shared agent_db fixture
(test_agents/conftest.py), which already points sysadmin_store.DB_PATH
at a throwaway file and calls init_db().
"""
from agents.sys_admin import sysadmin_report, sysadmin_store


def _report(module="infra_monitor", severity="info"):
    return sysadmin_report.build(module=module, action="check", reason="r", confidence=80, evidence={}, severity=severity)


class TestInitDb:
    def test_is_idempotent(self, agent_db):
        sysadmin_store.init_db()
        sysadmin_store.init_db()


class TestRecordAndListReports:
    def test_round_trip(self, agent_db):
        row_id = sysadmin_store.record_report(_report())
        assert row_id == 1
        hits = sysadmin_store.list_reports(module="infra_monitor")
        assert len(hits) == 1
        assert hits[0]["severity"] == "info"

    def test_filters_by_severity(self, agent_db):
        sysadmin_store.record_report(_report(severity="warning"))
        sysadmin_store.record_report(_report(severity="critical"))
        assert len(sysadmin_store.list_reports(severity="critical")) == 1

    def test_orders_most_recent_first(self, agent_db):
        sysadmin_store.record_report(sysadmin_report.build(
            module="m", action="first", reason="r", confidence=1, evidence={},
        ))
        sysadmin_store.record_report(sysadmin_report.build(
            module="m", action="second", reason="r", confidence=1, evidence={},
        ))
        hits = sysadmin_store.list_reports(module="m")
        assert [h["report_json"]["action"] for h in hits] == ["second", "first"]


class TestAgentStatus:
    def test_insert_on_first_upsert(self, agent_db):
        sysadmin_store.upsert_agent_status("dev_agent", enabled=True, last_heartbeat_ts="t1")
        status = sysadmin_store.get_agent_status("dev_agent")
        assert status["enabled"] == 1
        assert status["last_heartbeat_ts"] == "t1"
        assert status["crashed"] == 0

    def test_partial_update_preserves_other_fields(self, agent_db):
        sysadmin_store.upsert_agent_status("dev_agent", enabled=True, last_heartbeat_ts="t1")
        sysadmin_store.upsert_agent_status("dev_agent", crashed=True, crash_reason="boom")
        status = sysadmin_store.get_agent_status("dev_agent")
        assert status["enabled"] == 1  # untouched by the second call
        assert status["last_heartbeat_ts"] == "t1"  # untouched
        assert status["crashed"] == 1
        assert status["crash_reason"] == "boom"

    def test_unknown_agent_returns_none(self, agent_db):
        assert sysadmin_store.get_agent_status("nope") is None

    def test_clearing_crashed_also_clears_the_stale_reason(self, agent_db):
        sysadmin_store.upsert_agent_status("dev_agent", crashed=True, crash_reason="boom")
        sysadmin_store.upsert_agent_status("dev_agent", crashed=False)
        status = sysadmin_store.get_agent_status("dev_agent")
        assert status["crashed"] == 0
        assert status["crash_reason"] is None

    def test_setting_crashed_true_without_a_reason_does_not_wipe_an_existing_one(self, agent_db):
        sysadmin_store.upsert_agent_status("dev_agent", crashed=True, crash_reason="boom")
        sysadmin_store.upsert_agent_status("dev_agent", crashed=True)  # caller forgot the reason this time
        status = sysadmin_store.get_agent_status("dev_agent")
        assert status["crash_reason"] == "boom"

    def test_list_agent_status_returns_all(self, agent_db):
        sysadmin_store.upsert_agent_status("dev_agent", enabled=True)
        sysadmin_store.upsert_agent_status("risk_manager", enabled=False)
        statuses = sysadmin_store.list_agent_status()
        assert {s["agent"] for s in statuses} == {"dev_agent", "risk_manager"}


class TestBackups:
    def test_record_and_list(self, agent_db):
        row_id = sysadmin_store.record_backup(
            backup_path="/tmp/b1.db", source_db_path="oi_history.db", size_bytes=1024,
            verified=True, integrity_ok=True,
        )
        assert row_id == 1
        hits = sysadmin_store.list_backups()
        assert len(hits) == 1
        assert hits[0]["verified"] == 1

    def test_verified_only_filter(self, agent_db):
        sysadmin_store.record_backup(
            backup_path="/tmp/good.db", source_db_path="oi_history.db", size_bytes=1, verified=True, integrity_ok=True,
        )
        sysadmin_store.record_backup(
            backup_path="/tmp/bad.db", source_db_path="oi_history.db", size_bytes=1, verified=True, integrity_ok=False,
        )
        sysadmin_store.record_backup(
            backup_path="/tmp/unverified.db", source_db_path="oi_history.db", size_bytes=1, verified=False,
        )
        hits = sysadmin_store.list_backups(verified_only=True)
        assert len(hits) == 1
        assert hits[0]["backup_path"] == "/tmp/good.db"
