"""
Production Hardening Sprint -- "Memory leak detection" and "Stress
testing" extended beyond Milestone 8's own Module 10
(test_agents/sys_admin/test_production_readiness.py, which already
covers orchestrator.registry_snapshot(), SysAdminReport construction,
and concurrent writers against the sys_admin tables specifically).
This file probes surfaces that suite doesn't: risk_engine's pure math
under repetition, risk_store/supervision_store write paths, the
SystemAdministrator agent's own orchestration check, and concurrency
across MULTIPLE stores sharing one SQLite file simultaneously (closer
to a real mixed workload than any single store hammered alone).
"""
import concurrent.futures
import os

from agents import audit_log
from agents.memory.sqlite_store import SQLiteMemoryStore
from agents.risk_manager import risk_engine, risk_report, risk_store
from agents.sys_admin import admin_agent, maintenance
from agents.trading_supervisor import supervision_store


class TestMemoryLeakExtended:
    def test_risk_engine_pure_math_repeated_scoring_does_not_leak(self):
        points = [round(50 * ((-1) ** i) * (1 + i % 7), 1) for i in range(300)]
        checks = [risk_engine.position_sizing_check(120, capital=500000, risk_pct=1.0)]

        def one_cycle():
            var = risk_engine.value_at_risk(points, 0.95)
            cvar = risk_engine.expected_shortfall(points, 0.95)
            drawdown = risk_engine.simulate_drawdown_distribution(points, trials=100, percentile=95)
            stress = risk_engine.stress_test(points, (-0.3, -0.5))
            risk_engine.compute_risk_score(
                checks, var_pct_of_capital=abs(var) / 500000 * 100, cvar_pct_of_capital=abs(cvar) / 500000 * 100,
                drawdown_sim_pct_of_capital=abs(drawdown["percentile"]) / 500000 * 100,
                worst_stress_pct_of_capital=max(abs(v["max_drawdown"]) for v in stress.values()) / 500000 * 100,
                correlation_flags=0,
            )

        result = maintenance.probe_memory_leak(one_cycle, iterations=100)
        assert result["leak_suspected"] is False, (
            f"repeated risk_engine scoring grew memory by {result['growth_kb']}KB over "
            f"{result['iterations']} iterations -- investigate before relying on this in a long-lived process"
        )

    def test_risk_store_repeated_snapshot_writes_do_not_leak(self, agent_db):
        def one_cycle():
            report = risk_report.from_portfolio_snapshot({"summary": "probe"}, subject="probe")
            risk_store.record_snapshot(report, exposure=1.0, portfolio_heat=2.0)

        result = maintenance.probe_memory_leak(one_cycle, iterations=100)
        assert result["leak_suspected"] is False, (
            f"repeated risk_store writes grew memory by {result['growth_kb']}KB over "
            f"{result['iterations']} iterations -- possible unclosed connection or accumulating handle"
        )

    def test_admin_agent_orchestration_findings_repeated_do_not_leak(self, agent_db, tmp_path):
        """Deliberately NOT admin_agent.run_cycle() as a whole -- that
        also runs _security_findings()/_maintenance_findings(), which
        shell out to git/vulture/pip-audit per call and would make this
        probe a subprocess-spawn benchmark, not a memory-leak one.
        _orchestration_findings() is the pure in-process part."""
        store = SQLiteMemoryStore(db_path=str(tmp_path / "memory.db"))
        agent = admin_agent.SystemAdministrator(audit_log, memory_store=store, db_path=agent_db)

        result = maintenance.probe_memory_leak(agent._orchestration_findings, iterations=50)
        assert result["leak_suspected"] is False, (
            f"repeated orchestration checks grew memory by {result['growth_kb']}KB over "
            f"{result['iterations']} iterations"
        )


class TestStressExtended:
    def test_concurrent_writers_across_multiple_stores_sharing_one_db(self, agent_db):
        """A closer proxy to a real mixed workload than hammering one
        store's table alone (Milestone 8's own stress test): risk_store,
        supervision_store, and audit_log ALL writing concurrently to the
        SAME SQLite file at once, real OS threads, same busy_timeout
        hardening under real cross-table contention."""
        writers_per_store = 15

        def write_risk(i):
            report = risk_report.from_portfolio_snapshot({"summary": f"batch {i}"}, subject="probe")
            risk_store.record_snapshot(report, exposure=float(i))

        def write_supervision(i):
            from agents.trading_supervisor.supervision_report import SupervisionReport
            report = SupervisionReport(
                subject=f"cand_{i}", decision="APPROVED", summary=f"batch {i}", details={"i": i},
            )
            supervision_store.record_supervision(report, candidate_name=f"cand_{i}", symbol="NIFTY")

        def write_audit(i):
            audit_log.record(
                agent="stress_test", action_type="finding", description=f"batch {i}",
                risk_tier="READ_ONLY", outcome="approved",
            )

        with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
            futures = []
            for i in range(writers_per_store):
                futures.append(pool.submit(write_risk, i))
                futures.append(pool.submit(write_supervision, i))
                futures.append(pool.submit(write_audit, i))
            for f in futures:
                f.result(timeout=30)

        assert len(risk_store.list_snapshots(limit=1000)) == writers_per_store
        assert len(supervision_store.list_supervision_log(limit=1000)) == writers_per_store
        assert len(audit_log.list_recent(agent="stress_test", limit=1000)) == writers_per_store


class TestRecoveryExtended:
    def test_recovery_handles_a_zero_byte_database_file(self, agent_db, tmp_path):
        """A zero-byte file (e.g. a crashed write that truncated before
        writing anything) is a distinct corruption shape from a
        truncated-mid-content file (already covered by Milestone 8's
        own production-readiness suite) -- both must be detected, never
        crash the detector."""
        from agents.sys_admin import self_healing
        db_path = str(tmp_path / "zero_byte.db")
        open(db_path, "wb").close()
        assert os.path.getsize(db_path) == 0

        report = self_healing.propose_database_recovery(db_path=db_path)
        assert report.severity == "critical"

    def test_backup_creation_handles_a_missing_backup_directory_tree(self, agent_db, tmp_path, monkeypatch):
        """backup_dir doesn't exist yet, not even its parent -- a real
        "first backup ever taken on a fresh deployment" shape."""
        from agents.sys_admin import backup_recovery
        source = str(tmp_path / "source.db")
        import sqlite3
        conn = sqlite3.connect(source)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

        deep_backup_dir = str(tmp_path / "does" / "not" / "exist" / "yet")
        backup_id, report = backup_recovery.create_backup(source_db_path=source, backup_dir=deep_backup_dir)
        assert backup_id is not None
        assert report.severity == "info"
