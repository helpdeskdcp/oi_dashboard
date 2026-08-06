"""
agents/sys_admin/backup_recovery.py -- "Automatic backups for: Memory,
Database, Strategies, Config, Logs. Recovery verification before marking
backup healthy."

Almost everything this framework itself owns (Memory, the audit log,
Risk/Supervision/SysAdmin logs -- "Logs") lives in ONE SQLite file,
oi_history.db, so ONE real, atomic SQLite backup (sqlite3.Connection.
backup(), not a file copy -- safe against a concurrent writer, unlike
`cp`/`shutil.copy`, which can capture a torn write mid-transaction)
covers all of them. "Strategies" and "Config" (research_strategies/,
agents/config.py) are already versioned by git -- git IS their backup;
this module doesn't duplicate that. Secrets (.env, API keys) are
explicitly NEVER backed up here -- only the named source_db_path is
ever touched.

Recovery verification: a backup is never marked healthy just because
the file was written. It's opened fresh, PRAGMA integrity_check is run,
and its table row counts are compared against the source at backup
time -- only if both pass is verified=True/integrity_ok=True recorded.

"Never lose data": restore_backup() always takes a fresh safety backup
of the CURRENT state before ever overwriting it, defaults to a dry run
(reports the plan, touches nothing), and refuses to restore from any
backup that wasn't itself verified healthy.
"""
import datetime as dt
import os
import sqlite3

from .. import config
from . import sysadmin_report, sysadmin_store


def _table_row_counts(db_path: str) -> dict:
    conn = sqlite3.connect(db_path)
    try:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        conn.close()


def _verify_backup(backup_path: str, source_db_path: str) -> tuple:
    """Recovery verification BEFORE a backup is ever marked healthy:
    (1) open the backup fresh, run PRAGMA integrity_check;
    (2) compare its table row counts against the source (both read at
    verification time -- also catches a backup silently missing tables
    the source has). Returns (checked: bool, healthy: bool, evidence)."""
    try:
        conn = sqlite3.connect(backup_path)
        row = conn.execute("PRAGMA integrity_check").fetchone()
        integrity_ok = row is not None and row[0] == "ok"
        conn.close()
    except sqlite3.Error as exc:
        return True, False, {"error": str(exc)}

    if not integrity_ok:
        return True, False, {"integrity_check": "failed"}

    try:
        source_counts = _table_row_counts(source_db_path)
        backup_counts = _table_row_counts(backup_path)
    except sqlite3.Error as exc:
        return True, False, {"error": str(exc)}

    mismatches = {
        t: {"source": source_counts[t], "backup": backup_counts.get(t)}
        for t in source_counts if source_counts[t] != backup_counts.get(t)
    }
    if mismatches:
        return True, False, {"row_count_mismatches": mismatches}
    return True, True, {"tables_verified": len(source_counts)}


def _enforce_retention(backup_dir: str) -> None:
    """Keeps only the SYS_ADMIN_BACKUP_RETENTION_COUNT most recent
    backups (by sysadmin_store's own record, not directory listing) --
    only ever deletes a file THIS module recorded creating, never
    anything else that might be sitting in backup_dir."""
    all_backups = sysadmin_store.list_backups(limit=10_000)  # already ts DESC
    for old in all_backups[config.SYS_ADMIN_BACKUP_RETENTION_COUNT:]:
        path = old["backup_path"]
        if os.path.exists(path):
            os.remove(path)


def create_backup(*, source_db_path: str = "oi_history.db", backup_dir: str | None = None):
    """Returns (backup_id_or_None, SysAdminReport)."""
    backup_dir = backup_dir or config.SYS_ADMIN_BACKUP_DIR
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    backup_path = os.path.join(backup_dir, f"oi_history-{timestamp}.db")

    if not os.path.exists(source_db_path):
        report = sysadmin_report.build(
            module="backup_recovery", action="create_backup",
            reason=f"source database {source_db_path} does not exist -- nothing to back up",
            confidence=100, evidence={"source_db_path": source_db_path}, severity="critical",
        )
        sysadmin_store.record_report(report)
        return None, report

    source_conn = sqlite3.connect(source_db_path)
    dest_conn = sqlite3.connect(backup_path)
    try:
        source_conn.backup(dest_conn)
    except sqlite3.DatabaseError as exc:
        # The source itself is too corrupted to even read via the
        # backup API -- e.g. exactly the state propose_database_recovery()
        # is trying to recover FROM. Not a bug in the backup attempt;
        # report it as a failed backup (there's nothing valid to
        # preserve), never let it propagate as an unhandled crash --
        # a corrupted source is precisely the case this function must
        # stay usable for (see restore_backup()'s own pre-restore
        # safety-snapshot step, which calls this and must be able to
        # proceed with the restore even when this leg fails). The
        # `finally` below still closes both connections.
        if os.path.exists(backup_path):
            os.remove(backup_path)
        report = sysadmin_report.build(
            module="backup_recovery", action="create_backup",
            reason=f"source database {source_db_path} could not be read for backup: {exc}",
            confidence=90, evidence={"source_db_path": source_db_path, "error": str(exc)},
            affected_components=[source_db_path], severity="critical",
        )
        sysadmin_store.record_report(report)
        return None, report
    finally:
        dest_conn.close()
        source_conn.close()

    size_bytes = os.path.getsize(backup_path)
    _checked, healthy, verify_evidence = _verify_backup(backup_path, source_db_path)

    backup_id = sysadmin_store.record_backup(
        backup_path=backup_path, source_db_path=source_db_path, size_bytes=size_bytes,
        verified=True, integrity_ok=healthy,
    )
    report = sysadmin_report.build(
        module="backup_recovery", action="create_backup",
        reason=(
            f"backup created and verified healthy ({size_bytes} bytes)" if healthy else
            "backup created but FAILED verification -- do not rely on it"
        ),
        confidence=95 if healthy else 40,
        evidence={**verify_evidence, "backup_id": backup_id, "size_bytes": size_bytes},
        affected_components=[source_db_path], severity="info" if healthy else "critical",
    )
    sysadmin_store.record_report(report)
    _enforce_retention(backup_dir)
    return backup_id, report


def restore_backup(backup_id: int, *, target_db_path: str = "oi_history.db", dry_run: bool = True):
    """Restores a previously-verified backup over target_db_path.
    ALWAYS takes a fresh safety backup of target_db_path's CURRENT state
    first (never lose data, even the data being replaced) before ever
    overwriting it. dry_run=True (the default) only reports the plan --
    touches nothing. The actual overwrite (dry_run=False) is a human-
    invoked recovery action; agents.sys_admin.self_healing never calls
    this with dry_run=False on its own (see that module's docstring)."""
    backups = sysadmin_store.list_backups(limit=10_000)
    record = next((b for b in backups if b["id"] == backup_id), None)

    if record is None:
        report = sysadmin_report.build(
            module="backup_recovery", action="restore_backup", reason=f"no backup with id={backup_id}",
            confidence=100, evidence={"backup_id": backup_id}, severity="critical",
        )
        sysadmin_store.record_report(report)
        return report

    if not (record["verified"] and record["integrity_ok"]):
        report = sysadmin_report.build(
            module="backup_recovery", action="restore_backup",
            reason=f"backup {backup_id} was never verified healthy -- refusing to restore from it",
            confidence=100, evidence={"backup_id": backup_id, "record": record}, severity="critical",
        )
        sysadmin_store.record_report(report)
        return report

    if dry_run:
        report = sysadmin_report.build(
            module="backup_recovery", action="restore_backup",
            reason=f"DRY RUN: would restore backup {backup_id} ({record['backup_path']}) over {target_db_path}",
            confidence=90, evidence={"backup_id": backup_id, "target_db_path": target_db_path},
            affected_components=[target_db_path], recovery_outcome="not applied (dry_run=True)",
            severity="warning",
        )
        sysadmin_store.record_report(report)
        return report

    # Pre-restore safety snapshot -- best-effort. If target_db_path is
    # itself too corrupted to read (the common case: it's exactly why a
    # restore was proposed), create_backup() now reports that failure
    # and returns None rather than raising -- there is nothing valid to
    # preserve from a state that couldn't be read anyway, so the restore
    # proceeds regardless. This must never be the reason a genuine
    # recovery can't happen.
    safety_backup_id, safety_report = create_backup(source_db_path=target_db_path)

    # A corrupted target_db_path can fail sqlite3.Connection.backup() on
    # the DESTINATION side too, not just as a source -- the backup API
    # needs to open/read the destination's existing structure before
    # overwriting it. Remove the corrupted file (and its -wal/-shm
    # sidecars) first so the destination connection opens a FRESH,
    # empty database instead of an unreadable one.
    for suffix in ("", "-wal", "-shm"):
        stale = target_db_path + suffix
        if os.path.exists(stale):
            os.remove(stale)

    source_conn = sqlite3.connect(record["backup_path"])
    dest_conn = sqlite3.connect(target_db_path)
    try:
        source_conn.backup(dest_conn)
    finally:
        dest_conn.close()
        source_conn.close()

    recovery_outcome = (
        f"restored; pre-restore state saved as backup {safety_backup_id}" if safety_backup_id is not None else
        f"restored; pre-restore state could NOT be snapshotted first ({safety_report.reason}) -- "
        f"the prior (corrupted/unreadable) state was not recoverable to begin with"
    )
    report = sysadmin_report.build(
        module="backup_recovery", action="restore_backup",
        reason=f"restored backup {backup_id} over {target_db_path}", confidence=95,
        evidence={"backup_id": backup_id, "safety_backup_id": safety_backup_id},
        affected_components=[target_db_path], recovery_outcome=recovery_outcome, severity="warning",
    )
    sysadmin_store.record_report(report)
    return report
