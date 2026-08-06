"""
test_agents/sys_admin/test_self_healing.py -- regression tests for
self_healing.py. The central assertion running through this file: only
heal_agent_crash() and a successful retry_transient_connection() ever
actually change anything outside sysadmin's own bookkeeping tables --
every "propose_*" function only ever records a recommendation.
"""
import sqlite3

from agents.sys_admin import orchestrator, self_healing, sysadmin_store


class TestHealAgentCrash:
    def test_clears_the_crashed_flag(self, agent_db):
        orchestrator.record_crash("risk_manager", reason="boom")
        self_healing.heal_agent_crash("risk_manager")
        status = sysadmin_store.get_agent_status("risk_manager")
        assert status["crashed"] == 0


class TestRetryTransientConnection:
    def test_succeeds_against_a_real_healthy_db(self, agent_db, tmp_path):
        db_path = str(tmp_path / "t.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()
        result = self_healing.retry_transient_connection(db_path, attempts=3, backoff_seconds=0.01)
        assert result["recovered"] is True
        assert result["attempts_used"] == 1

    def test_fails_against_an_unreachable_path_and_escalates(self, agent_db, tmp_path):
        # A directory (not a file) as the "db path" reliably fails every
        # sqlite3.connect() attempt without needing a real lock/outage.
        bad_path = str(tmp_path / "not_a_file")
        bad_path_dir = tmp_path / "not_a_file"
        bad_path_dir.mkdir()
        result = self_healing.retry_transient_connection(bad_path, attempts=2, backoff_seconds=0.01)
        assert result["recovered"] is False
        assert result["report"].severity == "critical"


class TestProposeDatabaseRecovery:
    def test_healthy_db_needs_no_recovery(self, agent_db, tmp_path):
        db_path = str(tmp_path / "t.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()
        report = self_healing.propose_database_recovery(db_path=db_path)
        assert report.severity == "info"

    def test_missing_db_with_no_backup_reports_no_action_possible(self, agent_db, tmp_path):
        report = self_healing.propose_database_recovery(db_path=str(tmp_path / "nope.db"))
        assert report.severity == "critical"
        assert "no automatic action possible" in report.recovery_outcome

    def test_missing_db_with_a_verified_backup_proposes_restore_without_writing(self, agent_db, tmp_path):
        # Register a verified backup pointing at a real, healthy file --
        # propose_database_recovery must recommend it WITHOUT ever
        # touching the (missing/corrupt) target.
        good_backup = tmp_path / "good_backup.db"
        conn = sqlite3.connect(str(good_backup))
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()
        sysadmin_store.record_backup(
            backup_path=str(good_backup), source_db_path="oi_history.db", size_bytes=1,
            verified=True, integrity_ok=True,
        )
        target = str(tmp_path / "missing_target.db")

        report = self_healing.propose_database_recovery(db_path=target)

        assert report.severity == "critical"
        assert "recommending restore" in report.reason
        assert "proposed only" in report.recovery_outcome
        import os
        assert not os.path.exists(target)  # never actually restored


class TestProposeServiceRecovery:
    def test_always_proposes_never_restarts_anything(self, agent_db):
        report = self_healing.propose_service_recovery(reason="app.py appears unresponsive", evidence={"x": 1})
        assert report.severity == "critical"
        assert "no automatic service restart" in report.recovery_outcome


class TestProposeDeploymentRecovery:
    def test_never_calls_rollback_itself(self, agent_db, monkeypatch):
        from agents.sys_admin import deployment_manager
        called = {}
        monkeypatch.setattr(deployment_manager, "rollback", lambda *a, **k: called.setdefault("called", True))

        self_healing.propose_deployment_recovery(audit_log_id=7, reason="deployment failed")

        assert "called" not in called


class TestProposeConfigRecovery:
    def test_reports_but_never_reverts_anything(self, agent_db):
        report = self_healing.propose_config_recovery(modified_files=["agents/config.py"])
        assert report.severity == "warning"
        assert "human must" in report.recovery_outcome
