"""
test_agents/sys_admin/test_backup_recovery.py -- regression tests for
backup_recovery.py against real (throwaway) SQLite files -- never this
repo's real oi_history.db.
"""
import sqlite3

from agents.sys_admin import backup_recovery, sysadmin_store


def _make_db(path, *, rows=3):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, value TEXT)")
    for i in range(rows):
        conn.execute("INSERT INTO t (value) VALUES (?)", (f"v{i}",))
    conn.commit()
    conn.close()


class TestCreateBackup:
    def test_healthy_backup_is_verified(self, agent_db, tmp_path):
        source = str(tmp_path / "source.db")
        _make_db(source)
        backup_dir = str(tmp_path / "backups")

        backup_id, report = backup_recovery.create_backup(source_db_path=source, backup_dir=backup_dir)

        assert backup_id is not None
        assert report.severity == "info"
        hits = sysadmin_store.list_backups(verified_only=True)
        assert len(hits) == 1
        assert hits[0]["source_db_path"] == source

    def test_backup_contains_the_same_rows_as_the_source(self, agent_db, tmp_path):
        source = str(tmp_path / "source.db")
        _make_db(source, rows=5)
        backup_id, report = backup_recovery.create_backup(
            source_db_path=source, backup_dir=str(tmp_path / "backups"),
        )
        backup_path = sysadmin_store.list_backups()[0]["backup_path"]
        conn = sqlite3.connect(backup_path)
        count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        conn.close()
        assert count == 5

    def test_missing_source_is_a_critical_report_and_no_backup(self, agent_db, tmp_path):
        backup_id, report = backup_recovery.create_backup(
            source_db_path=str(tmp_path / "nope.db"), backup_dir=str(tmp_path / "backups"),
        )
        assert backup_id is None
        assert report.severity == "critical"
        assert sysadmin_store.list_backups() == []

    def test_retention_prunes_old_backups(self, agent_db, tmp_path, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "SYS_ADMIN_BACKUP_RETENTION_COUNT", 2)
        source = str(tmp_path / "source.db")
        _make_db(source)
        backup_dir = str(tmp_path / "backups")

        for _ in range(4):
            backup_recovery.create_backup(source_db_path=source, backup_dir=backup_dir)

        hits = sysadmin_store.list_backups(limit=100)
        assert len(hits) == 4  # records are never deleted, only files
        import os
        remaining_files = [f for f in os.listdir(backup_dir) if f.endswith(".db")]
        assert len(remaining_files) == 2


class TestRestoreBackup:
    def test_unknown_backup_id_is_a_critical_report(self, agent_db):
        report = backup_recovery.restore_backup(999)
        assert report.severity == "critical"
        assert "no backup" in report.reason

    def test_unverified_backup_refuses_to_restore(self, agent_db):
        backup_id = sysadmin_store.record_backup(
            backup_path="/tmp/whatever.db", source_db_path="x.db", size_bytes=1,
            verified=True, integrity_ok=False,
        )
        report = backup_recovery.restore_backup(backup_id)
        assert report.severity == "critical"
        assert "never verified healthy" in report.reason

    def test_dry_run_touches_nothing(self, agent_db, tmp_path):
        source = str(tmp_path / "source.db")
        _make_db(source, rows=3)
        backup_id, _r = backup_recovery.create_backup(source_db_path=source, backup_dir=str(tmp_path / "backups"))

        target = str(tmp_path / "target.db")
        _make_db(target, rows=1)  # different content -- must remain untouched
        report = backup_recovery.restore_backup(backup_id, target_db_path=target, dry_run=True)

        assert report.recovery_outcome == "not applied (dry_run=True)"
        conn = sqlite3.connect(target)
        count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        conn.close()
        assert count == 1  # unchanged

    def test_real_restore_overwrites_target_and_snapshots_first(self, agent_db, tmp_path):
        source = str(tmp_path / "source.db")
        _make_db(source, rows=3)
        backup_id, _r = backup_recovery.create_backup(source_db_path=source, backup_dir=str(tmp_path / "backups"))

        target = str(tmp_path / "target.db")
        _make_db(target, rows=1)
        before_backup_count = len(sysadmin_store.list_backups(limit=100))

        report = backup_recovery.restore_backup(backup_id, target_db_path=target, dry_run=False)

        assert report.severity == "warning"
        assert "restored" in report.recovery_outcome
        conn = sqlite3.connect(target)
        count = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
        conn.close()
        assert count == 3  # now matches the backup, not the original 1

        after_backup_count = len(sysadmin_store.list_backups(limit=100))
        assert after_backup_count == before_backup_count + 1  # the pre-restore safety snapshot
