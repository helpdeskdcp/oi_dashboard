"""
test_manage.py -- regression tests for manage.py, this project's
operational CLI (runtime-info / verify-backup). Written after finding a
real bug in this file's own first draft: a cwd-only + cmdline-substring
match for "the real app.py process" misidentified the shell invoking
manage.py itself as the target, because that shell's own wrapped
command string happens to contain the substring "app.py". Fixed with
exact-argv matching; these tests exist specifically so that regresses.
"""
import os
import sqlite3
import subprocess
import sys
import time

import pytest

import manage
import runtime_paths


class TestFindAppProcess:
    def test_finds_a_real_spawned_process_matching_cwd_and_exact_argv(self, tmp_path, monkeypatch):
        script = tmp_path / "app.py"
        script.write_text("import time\ntime.sleep(30)\n")
        proc = subprocess.Popen([sys.executable, "app.py"], cwd=str(tmp_path))
        try:
            time.sleep(0.3)  # let /proc/<pid>/cwd populate
            monkeypatch.setattr(runtime_paths, "APP_ROOT", str(tmp_path))
            found = manage._find_app_process()
            assert found is not None
            assert found["pid"] == proc.pid
            assert "app.py" in found["cmdline"]
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_does_not_match_a_process_whose_cmdline_only_contains_app_py_as_a_substring(self, tmp_path, monkeypatch):
        """The exact bug found during development: a shell process
        running from the target cwd, whose cmdline is a long string
        that happens to MENTION "app.py" (e.g. wrapping a command like
        "python3 manage.py ... app.py ...") must NOT be misidentified
        as the real app.py process -- only an exact argv element
        "app.py" (or ending in "/app.py") counts."""
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)  # decoy mentioning app.py in a comment"],
            cwd=str(tmp_path),
        )
        try:
            time.sleep(0.3)
            monkeypatch.setattr(runtime_paths, "APP_ROOT", str(tmp_path))
            found = manage._find_app_process()
            assert found is None or found["pid"] != proc.pid
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_does_not_match_a_real_app_py_process_from_a_different_cwd(self, tmp_path, monkeypatch):
        """cwd match matters too -- rules out an unrelated app.py
        elsewhere on a shared VPS (e.g. /root/camera_monitor/app.py)."""
        other_dir = tmp_path / "unrelated_project"
        other_dir.mkdir()
        script = other_dir / "app.py"
        script.write_text("import time\ntime.sleep(30)\n")
        proc = subprocess.Popen([sys.executable, "app.py"], cwd=str(other_dir))
        try:
            time.sleep(0.3)
            target_dir = tmp_path / "this_project"
            target_dir.mkdir()
            monkeypatch.setattr(runtime_paths, "APP_ROOT", str(target_dir))
            found = manage._find_app_process()
            assert found is None or found["pid"] != proc.pid
        finally:
            proc.terminate()
            proc.wait(timeout=5)

    def test_returns_none_when_nothing_matches(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runtime_paths, "APP_ROOT", str(tmp_path / "nobody_runs_here"))
        assert manage._find_app_process() is None


class TestSnapshotLoopActive:
    def test_false_when_database_does_not_exist(self, tmp_path, monkeypatch):
        monkeypatch.setattr(runtime_paths, "DATABASE_PATH", str(tmp_path / "nonexistent.db"))
        active, last_ts = manage._snapshot_loop_active()
        assert active is False
        assert last_ts is None

    def test_false_when_table_does_not_exist_yet(self, tmp_path, monkeypatch):
        db = tmp_path / "empty.db"
        sqlite3.connect(str(db)).close()
        monkeypatch.setattr(runtime_paths, "DATABASE_PATH", str(db))
        active, last_ts = manage._snapshot_loop_active()
        assert active is False
        assert last_ts is None

    def test_true_for_a_fresh_snapshot(self, tmp_path, monkeypatch):
        import datetime as dt
        db = tmp_path / "fresh.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE intelligence_snapshots_log (ts TEXT)")
        conn.execute("INSERT INTO intelligence_snapshots_log VALUES (?)", (dt.datetime.now().isoformat(),))
        conn.commit()
        conn.close()
        monkeypatch.setattr(runtime_paths, "DATABASE_PATH", str(db))
        active, last_ts = manage._snapshot_loop_active()
        assert active is True

    def test_false_for_a_stale_snapshot(self, tmp_path, monkeypatch):
        import datetime as dt
        db = tmp_path / "stale.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE intelligence_snapshots_log (ts TEXT)")
        old_ts = (dt.datetime.now() - dt.timedelta(hours=2)).isoformat()
        conn.execute("INSERT INTO intelligence_snapshots_log VALUES (?)", (old_ts,))
        conn.commit()
        conn.close()
        monkeypatch.setattr(runtime_paths, "DATABASE_PATH", str(db))
        active, last_ts = manage._snapshot_loop_active()
        assert active is False
        assert last_ts == old_ts


class TestVerifyBackup:
    def _make_valid_backup(self, tmp_path):
        db = tmp_path / "valid_backup.db"
        conn = sqlite3.connect(str(db))
        for table in manage.REQUIRED_TABLES:
            if table == "intelligence_snapshots_log":
                conn.execute(f"CREATE TABLE {table} (ts TEXT)")
                conn.execute(f"INSERT INTO {table} VALUES ('2026-08-10T10:00:00')")
            else:
                conn.execute(f"CREATE TABLE {table} (id INTEGER)")
        conn.commit()
        conn.close()
        return db

    def test_passes_for_a_genuinely_valid_backup(self, tmp_path, capsys):
        db = self._make_valid_backup(tmp_path)
        rc = manage._cmd_verify_backup(argparse_namespace(path=str(db)))
        assert rc == 0
        assert "PASSED" in capsys.readouterr().out

    def test_fails_when_file_does_not_exist(self, tmp_path, capsys):
        rc = manage._cmd_verify_backup(argparse_namespace(path=str(tmp_path / "nope.db")))
        assert rc == 1
        assert "does not exist" in capsys.readouterr().out

    def test_fails_for_a_non_sqlite_file(self, tmp_path, capsys):
        junk = tmp_path / "junk.db"
        junk.write_bytes(b"not a real sqlite database at all")
        rc = manage._cmd_verify_backup(argparse_namespace(path=str(junk)))
        assert rc == 1
        assert "bad header" in capsys.readouterr().out

    def test_fails_when_required_table_is_missing(self, tmp_path, capsys):
        db = tmp_path / "incomplete.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE users (id INTEGER)")
        conn.commit()
        conn.close()
        rc = manage._cmd_verify_backup(argparse_namespace(path=str(db)))
        assert rc == 1
        assert "missing required tables" in capsys.readouterr().out

    def test_does_not_compare_row_counts_against_a_live_database(self, tmp_path, capsys):
        """The exact false-positive this command exists to avoid: a
        backup with FEWER rows than some other (live) database must
        never fail verification on that basis alone -- this command
        only ever checks the backup file on its own terms."""
        db = self._make_valid_backup(tmp_path)
        rc = manage._cmd_verify_backup(argparse_namespace(path=str(db)))
        assert rc == 0
        source = open(manage.__file__).read()
        assert "row_count" not in source.lower()


def argparse_namespace(**kwargs):
    import argparse
    return argparse.Namespace(**kwargs)
