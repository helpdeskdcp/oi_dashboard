"""
test_agents/sys_admin/test_infra_monitor.py -- regression tests for
infra_monitor.py. Real /proc/meminfo, os.getloadavg(), shutil.disk_usage
reads (these are always safe, local, and fast on Linux -- the actual
platform this repo runs on); socket.create_connection is monkeypatched
so tests never make a real network call.
"""
import socket
import sqlite3

import pytest

from agents import config
from agents.sys_admin import infra_monitor


class TestCpuStatus:
    def test_returns_a_load1_per_core_ratio(self):
        result = infra_monitor.cpu_status()
        assert result["cores"] is None or result["cores"] >= 1
        if result["load1"] is not None:
            # approx, not exact: load1_per_core is computed from the raw
            # (unrounded) load average, while result["load1"] is itself
            # already rounded for display -- re-deriving from the rounded
            # value can differ by a double-rounding cent or two.
            assert result["load1_per_core"] == pytest.approx(result["load1"] / result["cores"], abs=0.01)


class TestMemoryStatus:
    def test_returns_a_used_pct_on_linux(self):
        result = infra_monitor.memory_status()
        # This repo's actual deployment platform is Linux -- /proc/meminfo
        # should be readable; if not (unexpected sandbox), degrade honestly.
        assert result["used_pct"] is None or 0 <= result["used_pct"] <= 100


class TestDiskStatus:
    def test_reports_a_used_pct_for_the_repo_filesystem(self, tmp_path):
        result = infra_monitor.disk_status(str(tmp_path))
        assert result["total_bytes"] > 0
        assert 0 <= result["used_pct"] <= 100


class TestGpuStatus:
    def test_reports_unavailable_when_no_nvidia_smi(self, monkeypatch):
        import shutil as shutil_mod
        monkeypatch.setattr(shutil_mod, "which", lambda name: None)
        result = infra_monitor.gpu_status()
        assert result["available"] is False


class TestNetworkStatus:
    def test_reachable_host_reports_latency(self, monkeypatch):
        class FakeSocket:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(socket, "create_connection", lambda addr, timeout: FakeSocket())
        result = infra_monitor.network_status(hosts=(("1.2.3.4", 80),))
        assert result["1.2.3.4"]["reachable"] is True
        assert result["1.2.3.4"]["latency_ms"] is not None

    def test_unreachable_host_reports_the_error(self, monkeypatch):
        def boom(addr, timeout):
            raise OSError("connection refused")

        monkeypatch.setattr(socket, "create_connection", boom)
        result = infra_monitor.network_status(hosts=(("1.2.3.4", 80),))
        assert result["1.2.3.4"]["reachable"] is False
        assert "connection refused" in result["1.2.3.4"]["error"]

    def test_empty_hosts_returns_empty(self):
        assert infra_monitor.network_status(hosts=()) == {}


class TestSqliteStatus:
    def test_missing_file_reports_does_not_exist(self, tmp_path):
        result = infra_monitor.sqlite_status(str(tmp_path / "nope.db"))
        assert result["exists"] is False

    def test_healthy_db_reports_integrity_ok(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()
        result = infra_monitor.sqlite_status(db_path)
        assert result["exists"] is True
        assert result["integrity_ok"] is True
        assert result["size_bytes"] > 0
        assert result["query_latency_ms"] is not None


class TestQueueLength:
    def test_counts_pending_approval_rows(self, agent_db):
        from agents import audit_log
        audit_log.record(agent="dev_agent", action_type="proposal", description="p",
                          risk_tier="needs_approval", outcome="pending_approval")
        audit_log.record(agent="dev_agent", action_type="proposal", description="p2",
                          risk_tier="needs_approval", outcome="approved")
        assert infra_monitor.queue_length() == 1


class TestThreadHealth:
    def test_includes_the_current_thread(self):
        import threading
        result = infra_monitor.thread_health()
        assert result["count"] >= 1
        assert threading.current_thread().name in result["names"]


class TestFindings:
    def test_no_findings_when_everything_is_healthy(self):
        findings = infra_monitor._findings(
            cpu={"load1_per_core": 0.1}, memory={"used_pct": 10.0}, disk={"used_pct": 10.0},
            sqlite_info={"integrity_ok": True, "query_latency_ms": 1.0}, queue_len=0,
        )
        assert findings == []

    def test_critical_memory_and_disk_are_flagged(self, monkeypatch):
        monkeypatch.setattr(config, "SYS_ADMIN_MEMORY_CRITICAL_PCT", 90.0)
        monkeypatch.setattr(config, "SYS_ADMIN_DISK_CRITICAL_PCT", 90.0)
        findings = infra_monitor._findings(
            cpu={"load1_per_core": 0.1}, memory={"used_pct": 95.0}, disk={"used_pct": 96.0},
            sqlite_info={"integrity_ok": True, "query_latency_ms": 1.0}, queue_len=0,
        )
        severities = {f.action: f.severity for f in findings}
        assert severities["memory_check"] == "critical"
        assert severities["disk_check"] == "critical"

    def test_failed_integrity_check_is_critical(self):
        findings = infra_monitor._findings(
            cpu={"load1_per_core": 0.1}, memory={"used_pct": 10.0}, disk={"used_pct": 10.0},
            sqlite_info={"integrity_ok": False, "query_latency_ms": 1.0}, queue_len=0,
        )
        assert any(f.action == "sqlite_check" and f.severity == "critical" for f in findings)

    def test_large_queue_is_flagged(self, monkeypatch):
        monkeypatch.setattr(config, "SYS_ADMIN_QUEUE_LENGTH_WARN", 5)
        findings = infra_monitor._findings(
            cpu={"load1_per_core": 0.1}, memory={"used_pct": 10.0}, disk={"used_pct": 10.0},
            sqlite_info={"integrity_ok": True, "query_latency_ms": 1.0}, queue_len=10,
        )
        assert any(f.action == "queue_length_check" for f in findings)


class TestSnapshot:
    def test_produces_a_complete_snapshot(self, agent_db, tmp_path):
        db_path = str(tmp_path / "t.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()
        result = infra_monitor.snapshot(db_path=db_path, disk_path=str(tmp_path), check_network=False)
        assert result.sqlite["exists"] is True
        assert result.network == {}
        assert isinstance(result.reports, list)

    def test_findings_are_persisted_to_sysadmin_store(self, agent_db, tmp_path, monkeypatch):
        from agents import config
        from agents.sys_admin import sysadmin_store
        monkeypatch.setattr(config, "SYS_ADMIN_QUEUE_LENGTH_WARN", -1)  # force at least one finding
        infra_monitor.snapshot(db_path=str(tmp_path / "nope.db"), disk_path=str(tmp_path), check_network=False)
        assert len(sysadmin_store.list_reports(module="infra_monitor")) >= 1
