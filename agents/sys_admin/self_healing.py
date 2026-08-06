"""
agents/sys_admin/self_healing.py -- "Recover automatically from: Agent
crash, Database failure, Service failure, Deployment failure,
Configuration corruption. Never lose data."

THE central safety decision of this package: automatic recovery is only
ever performed for actions that are non-destructive and fully reversible
by construction. Everything else is DETECTED and PROPOSED -- recorded
with a specific recommended action and full evidence -- never applied.
This is not a partial implementation of "self-healing"; it is the
correct scope for it in a framework whose every other agent (Milestones
1-7) has held to the same "propose, don't act" principle since
Milestone 1 (agents/base_agent.py has no apply/execute method anywhere;
agents.sys_admin.security_audit.check_propose_only_invariant verifies
that programmatically, not just in this docstring).

Safe to auto-heal (bookkeeping only -- no mutation beyond this
package's own tables, and never re-invokes whatever produced the
failure in the first place):
  - agent crash             -> orchestrator.restart_agent() clears the
                               crashed flag; the crashed OPERATION is
                               never automatically re-run.
  - transient DB contention  -> a bounded connection retry with backoff
                               (the connection, never a write's content).

Propose-only (recorded, never auto-applied -- each needs a human, or an
explicit, separately-authorized dry_run=False call):
  - database failure (corruption) -> recommends backup_recovery.
                                     restore_backup(dry_run=True by
                                     default -- never writes on its own).
  - service failure                -> no automatic app.py restart
                                     exists anywhere in this framework.
  - deployment failure               -> deployment_manager.rollback()
                                     is never called automatically here.
  - configuration corruption          -> flagged; agents/config.py is
                                     git-versioned, so recovery is
                                     `git checkout`, always a human call
                                     (reverting a config a human just
                                     intentionally changed would be
                                     actively harmful, not helpful).
"""
import os
import sqlite3
import time

from . import backup_recovery, orchestrator, sysadmin_report, sysadmin_store


def heal_agent_crash(agent: str):
    """The one fully-automatic recovery in this module: clears a
    recorded crash so the agent's next real trigger runs normally.
    Safe because it only mutates agents.sys_admin's own bookkeeping and
    never re-invokes the crashed operation itself."""
    return orchestrator.restart_agent(agent)


def retry_transient_connection(db_path: str, *, attempts: int = 3, backoff_seconds: float = 0.5) -> dict:
    """Safe to automate: retrying a CONNECTION (never a write's
    content) after a transient lock. The same idea as the busy_timeout
    hardening already applied to every agents.* SQLite connection, one
    layer up, for a caller that hit a hard failure despite that
    timeout."""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            conn.execute("SELECT 1")
            conn.close()
            report = sysadmin_report.build(
                module="self_healing", action="retry_transient_connection",
                reason=f"connection succeeded on attempt {attempt}/{attempts}", confidence=90,
                evidence={"db_path": db_path, "attempts_used": attempt},
                affected_components=[db_path], recovery_outcome="connection restored", severity="info",
            )
            sysadmin_store.record_report(report)
            return {"recovered": True, "attempts_used": attempt, "report": report}
        except sqlite3.Error as exc:
            last_error = exc
            time.sleep(backoff_seconds * attempt)

    report = sysadmin_report.build(
        module="self_healing", action="retry_transient_connection",
        reason=f"connection still failing after {attempts} attempts: {last_error}", confidence=80,
        evidence={"db_path": db_path, "attempts_used": attempts, "error": str(last_error)},
        affected_components=[db_path], recovery_outcome="not recovered -- escalating", severity="critical",
    )
    sysadmin_store.record_report(report)
    return {"recovered": False, "attempts_used": attempts, "report": report}


def propose_database_recovery(*, db_path: str = "oi_history.db"):
    """Detects DB corruption and PROPOSES the specific recovery action
    (restore from the most recent verified backup) -- never applies it.
    Calls backup_recovery.restore_backup with dry_run=True (the
    default), which itself never writes to target_db_path."""
    if not os.path.exists(db_path):
        # sqlite3.connect() silently CREATES a missing file rather than
        # raising -- an empty, freshly-created database trivially passes
        # integrity_check, which would otherwise make "the database is
        # gone" look identical to "the database is healthy." A missing
        # database is never healthy; check existence before ever
        # connecting.
        healthy = False
    else:
        conn = None
        try:
            conn = sqlite3.connect(db_path)
            row = conn.execute("PRAGMA integrity_check").fetchone()
            healthy = row is not None and row[0] == "ok"
        except sqlite3.Error:
            healthy = False
        finally:
            if conn:
                conn.close()

    if healthy:
        return sysadmin_report.build(
            module="self_healing", action="propose_database_recovery",
            reason=f"{db_path} passed integrity_check -- no recovery needed", confidence=95,
            evidence={"db_path": db_path}, severity="info",
        )

    candidates = sysadmin_store.list_backups(verified_only=True, limit=1)
    if not candidates:
        report = sysadmin_report.build(
            module="self_healing", action="propose_database_recovery",
            reason=f"{db_path} FAILED integrity_check and no verified backup exists to restore from",
            confidence=95, evidence={"db_path": db_path}, affected_components=[db_path],
            recovery_outcome="no automatic action possible -- create a verified backup as soon as the "
                              "database is healthy again", severity="critical",
        )
        sysadmin_store.record_report(report)
        return report

    backup_recovery.restore_backup(candidates[0]["id"], target_db_path=db_path, dry_run=True)
    report = sysadmin_report.build(
        module="self_healing", action="propose_database_recovery",
        reason=f"{db_path} FAILED integrity_check -- recommending restore from backup "
               f"{candidates[0]['id']} ({candidates[0]['backup_path']})",
        confidence=85, evidence={"db_path": db_path, "recommended_backup_id": candidates[0]["id"]},
        affected_components=[db_path],
        recovery_outcome="proposed only -- call backup_recovery.restore_backup(..., dry_run=False) "
                          "explicitly to apply", severity="critical",
    )
    sysadmin_store.record_report(report)
    return report


def propose_service_recovery(*, reason: str, evidence: dict):
    """No automatic service (app.py) restart exists anywhere in this
    framework -- this is always a human action. Detected, recorded,
    never automated."""
    report = sysadmin_report.build(
        module="self_healing", action="propose_service_recovery", reason=reason, confidence=70,
        evidence=evidence, affected_components=["app.py"],
        recovery_outcome="proposed only -- no automatic service restart exists in this framework",
        severity="critical",
    )
    sysadmin_store.record_report(report)
    return report


def propose_deployment_recovery(*, audit_log_id: int, reason: str):
    """Deployment-failure recovery is always
    agents.sys_admin.deployment_manager.rollback() -- which itself only
    ever calls agents/rollback.py's git revert. Never invoked
    automatically here; this only records the recommendation."""
    report = sysadmin_report.build(
        module="self_healing", action="propose_deployment_recovery", reason=reason, confidence=80,
        evidence={"audit_log_id": audit_log_id},
        recovery_outcome=f"proposed only -- call deployment_manager.rollback({audit_log_id}) explicitly to apply",
        severity="critical",
    )
    sysadmin_store.record_report(report)
    return report


def propose_config_recovery(*, modified_files: list):
    """Configuration-corruption recovery is `git checkout` on the
    affected file(s) -- a one-line, always-available, always-safe
    recovery since agents/config.py is fully git-versioned. Still never
    invoked automatically: reverting a config a human just intentionally
    changed would be actively harmful, not helpful."""
    report = sysadmin_report.build(
        module="self_healing", action="propose_config_recovery",
        reason=f"{len(modified_files)} config file(s) differ from the known-good ref", confidence=60,
        evidence={"modified_files": modified_files}, affected_components=modified_files,
        recovery_outcome="proposed only -- `git checkout <ref> -- <file>` restores it; a human must "
                          "confirm this wasn't an intentional change first",
        severity="warning",
    )
    sysadmin_store.record_report(report)
    return report
