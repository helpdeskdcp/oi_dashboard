"""
test_agents/sys_admin/test_maintenance.py -- regression tests for
maintenance.py against real (throwaway) files/SQLite databases.
"""
import sqlite3

from agents.sys_admin import maintenance, sysadmin_store


class TestFindDuplicateBlocks:
    def test_finds_a_block_repeated_across_two_files(self, tmp_path):
        block = "\n".join(f"line {i}" for i in range(6))
        (tmp_path / "a.py").write_text(block + "\nunique_a\n")
        (tmp_path / "b.py").write_text(block + "\nunique_b\n")
        duplicates = maintenance.find_duplicate_blocks(
            [str(tmp_path / "a.py"), str(tmp_path / "b.py")], min_lines=6,
        )
        assert len(duplicates) >= 1
        paths = {loc["path"] for d in duplicates for loc in d["locations"]}
        assert str(tmp_path / "a.py") in paths and str(tmp_path / "b.py") in paths

    def test_no_duplicates_across_distinct_files(self, tmp_path):
        (tmp_path / "a.py").write_text("\n".join(f"a{i}" for i in range(10)))
        (tmp_path / "b.py").write_text("\n".join(f"b{i}" for i in range(10)))
        assert maintenance.find_duplicate_blocks(
            [str(tmp_path / "a.py"), str(tmp_path / "b.py")], min_lines=6,
        ) == []

    def test_blank_line_runs_are_never_flagged(self, tmp_path):
        (tmp_path / "a.py").write_text("\n" * 20)
        (tmp_path / "b.py").write_text("\n" * 20)
        assert maintenance.find_duplicate_blocks(
            [str(tmp_path / "a.py"), str(tmp_path / "b.py")], min_lines=6,
        ) == []


class TestParsePytestDurations:
    def test_parses_real_duration_lines(self):
        output = (
            "================= slowest 3 durations ==================\n"
            "1.23s call     test_agents/sys_admin/test_foo.py::test_bar\n"
            "0.45s setup    test_agents/sys_admin/test_foo.py::test_baz\n"
            "= 5 passed in 2.10s =\n"
        )
        parsed = maintenance.parse_pytest_durations(output)
        assert len(parsed) == 2
        assert parsed[0] == {"seconds": 1.23, "phase": "call", "test": "test_agents/sys_admin/test_foo.py::test_bar"}

    def test_no_duration_lines_returns_empty(self):
        assert maintenance.parse_pytest_durations("no matches here\n") == []


class TestProbeMemoryLeak:
    def test_a_stable_operation_is_not_flagged(self):
        def stable():
            x = [1, 2, 3]
            return sum(x)

        result = maintenance.probe_memory_leak(stable, iterations=20)
        assert result["iterations"] == 20
        assert result["leak_suspected"] is False

    def test_a_genuinely_growing_list_is_flagged(self, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "SYS_ADMIN_MEMORY_LEAK_GROWTH_KB", 1.0)  # tiny threshold, easy to exceed
        leaked = []

        def leaking():
            leaked.append("x" * 10_000)  # genuinely retained every call

        result = maintenance.probe_memory_leak(leaking, iterations=50)
        assert result["leak_suspected"] is True
        assert result["growth_kb"] > 0


class TestCheckDbFragmentation:
    def test_missing_db_is_not_checked(self, tmp_path):
        result = maintenance.check_db_fragmentation(str(tmp_path / "nope.db"))
        assert result["checked"] is False

    def test_fresh_db_has_low_fragmentation(self, tmp_path):
        db_path = str(tmp_path / "t.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()
        result = maintenance.check_db_fragmentation(db_path)
        assert result["checked"] is True
        assert result["vacuum_recommended"] is False


class TestCheckDependencies:
    def test_degrades_gracefully_without_pip_audit(self, monkeypatch):
        import shutil as shutil_mod
        monkeypatch.setattr(shutil_mod, "which", lambda name: None)
        result = maintenance.check_dependencies()
        assert result["checked"] is False


class TestRunMaintenanceSweep:
    def test_clean_state_produces_minimal_findings(self, agent_db, tmp_path, monkeypatch):
        import shutil as shutil_mod
        monkeypatch.setattr(shutil_mod, "which", lambda name: None)  # no vulture/pip-audit
        db_path = str(tmp_path / "t.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()

        reports = maintenance.run_maintenance_sweep(source_paths=[], db_path=db_path)
        assert reports == []

    def test_duplicate_blocks_produce_a_report(self, agent_db, tmp_path, monkeypatch):
        import shutil as shutil_mod
        monkeypatch.setattr(shutil_mod, "which", lambda name: None)
        block = "\n".join(f"line {i}" for i in range(8))
        (tmp_path / "a.py").write_text(block)
        (tmp_path / "b.py").write_text(block)

        reports = maintenance.run_maintenance_sweep(
            source_paths=[str(tmp_path / "a.py"), str(tmp_path / "b.py")], db_path=str(tmp_path / "nope.db"),
        )
        assert any(r.action == "duplicate_code_check" for r in reports)
        assert len(sysadmin_store.list_reports(module="maintenance")) == len(reports)

    def test_slow_tests_from_pytest_output_produce_a_report(self, agent_db, tmp_path, monkeypatch):
        import shutil as shutil_mod
        monkeypatch.setattr(shutil_mod, "which", lambda name: None)
        output = "5.00s call     test_agents/sys_admin/test_slow.py::test_slow\n"
        reports = maintenance.run_maintenance_sweep(
            source_paths=[], db_path=str(tmp_path / "nope.db"), pytest_output=output,
        )
        assert any(r.action == "slow_test_check" for r in reports)
