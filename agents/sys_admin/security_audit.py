"""
agents/sys_admin/security_audit.py -- "Secret management. API key
validation. Permission audit. Unexpected code modification detection.
Integrity verification."

Secret scanning reuses agents.dev_agent.sanitizer's own pattern list
(sanitizer.find_matches, added this milestone) rather than a second set
of secret-detection regexes. API key validation reuses each
agents.llm_providers adapter's own is_configured() -- never a live call
to actually verify a key works (that would burn API quota for a "just
checking" audit, and is exactly the kind of unnecessary external call
this framework avoids everywhere else -- see agents.risk_manager.
data_access's and agents.trading_supervisor.data_health's own
"never call the live broker" landmine notes). Permission audit checks
the propose-only architectural invariant PROGRAMMATICALLY, not just in
a docstring. Unexpected code modification detection is git-based:
whatever differs between config.SYS_ADMIN_KNOWN_GOOD_REF and the working
tree is reported, never judged -- an in-flight, expected change looks
identical to this check as an actual compromise; a human decides which.
"""
import os
import subprocess

from .. import base_agent, config, llm_providers
from ..dev_agent import sanitizer
from . import sysadmin_report, sysadmin_store


def scan_for_secrets(paths: list) -> list:
    """Real-file secret scan. Returns [{"path", "pattern"}, ...] -- never
    the matched TEXT itself, which would defeat the point of a secret
    scan by putting the secret in the report."""
    findings = []
    for path in paths:
        try:
            with open(path, "r", errors="replace") as fh:
                content = fh.read()
        except OSError:
            continue
        for name in sanitizer.find_matches(content):
            findings.append({"path": path, "pattern": name})
    return findings


def validate_api_keys() -> dict:
    """Presence/format validation only, via each provider's own
    is_configured() -- never a live API call."""
    result = {}
    for name in llm_providers.available_providers():
        try:
            result[name] = llm_providers.get_llm_provider(name).is_configured()
        except Exception:
            result[name] = False
    return result


def check_propose_only_invariant() -> dict:
    """Verifies PROGRAMMATICALLY -- not just in a docstring -- that
    BaseAgent has no method that applies/executes/merges/deploys
    anything. There is no way to "behaviorally test" a method not
    existing other than confirming its name isn't present; any of these
    appearing on BaseAgent would be a direct violation of this
    framework's core safety principle, present in every milestone since
    Milestone 1."""
    forbidden = ("apply", "execute", "merge", "deploy", "commit_to_master")
    found = [name for name in forbidden if hasattr(base_agent.BaseAgent, name)]
    return {"forbidden_methods_found": found, "invariant_holds": not found}


def detect_unexpected_modifications(*, repo_dir: str = ".", ref: str | None = None) -> dict:
    """Whatever differs between config.SYS_ADMIN_KNOWN_GOOD_REF and the
    working tree, scoped to agents/ (the highest-trust code in this
    repo -- see config.SELF_MODIFICATION_GUARD_PREFIX). Reports the
    diff, never judges it."""
    ref = ref or config.SYS_ADMIN_KNOWN_GOOD_REF
    result = subprocess.run(
        ["git", "diff", "--name-only", ref, "--", config.SELF_MODIFICATION_GUARD_PREFIX],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {"checked": False, "error": result.stderr.strip(), "modified_files": []}
    return {"checked": True, "modified_files": [f for f in result.stdout.splitlines() if f]}


def check_integrity(*, db_path: str = "oi_history.db", repo_dir: str = ".") -> dict:
    """SQLite integrity (PRAGMA integrity_check) + git repo integrity
    (git fsck) -- the two stores of truth this framework depends on."""
    import sqlite3
    sqlite_ok = None
    if os.path.exists(db_path):
        try:
            conn = sqlite3.connect(db_path)
            row = conn.execute("PRAGMA integrity_check").fetchone()
            sqlite_ok = row is not None and row[0] == "ok"
            conn.close()
        except sqlite3.Error:
            sqlite_ok = False

    fsck = subprocess.run(["git", "fsck", "--no-progress"], cwd=repo_dir, capture_output=True, text=True)
    git_ok = fsck.returncode == 0 and not fsck.stdout.strip()

    return {"sqlite_integrity_ok": sqlite_ok, "git_fsck_ok": git_ok, "git_fsck_output": fsck.stdout.strip()}


def run_audit(*, repo_dir: str = ".", scan_paths: list | None = None, db_path: str = "oi_history.db") -> list:
    """The full Module 5 sweep -- returns a list of SysAdminReport, one
    per finding worth surfacing. A clean audit returns an empty list,
    not a fabricated "all clear" report."""
    reports = []

    if scan_paths:
        secrets = scan_for_secrets(scan_paths)
        if secrets:
            reports.append(sysadmin_report.build(
                module="security_audit", action="secret_scan",
                reason=f"{len(secrets)} potential secret(s) found in source files",
                confidence=70, evidence={"findings": secrets},
                affected_components=sorted({s["path"] for s in secrets}), severity="critical",
            ))

    key_status = validate_api_keys()
    if key_status and not any(key_status.values()):
        reports.append(sysadmin_report.build(
            module="security_audit", action="api_key_validation",
            reason="no LLM provider has a valid API key configured -- agents.dev_agent cannot function",
            confidence=90, evidence=key_status, affected_components=["dev_agent"], severity="critical",
        ))

    invariant = check_propose_only_invariant()
    if not invariant["invariant_holds"]:
        reports.append(sysadmin_report.build(
            module="security_audit", action="propose_only_invariant",
            reason=f"BaseAgent has gained forbidden method(s): {invariant['forbidden_methods_found']}",
            confidence=100, evidence=invariant, affected_components=["agents/base_agent.py"], severity="critical",
        ))

    mods = detect_unexpected_modifications(repo_dir=repo_dir)
    if mods["checked"] and mods["modified_files"]:
        reports.append(sysadmin_report.build(
            module="security_audit", action="code_modification_check",
            reason=f"{len(mods['modified_files'])} file(s) under agents/ differ from the known-good ref",
            confidence=60, evidence=mods, affected_components=mods["modified_files"], severity="warning",
        ))

    integrity = check_integrity(db_path=db_path, repo_dir=repo_dir)
    if integrity["sqlite_integrity_ok"] is False or integrity["git_fsck_ok"] is False:
        reports.append(sysadmin_report.build(
            module="security_audit", action="integrity_check",
            reason="SQLite integrity check or git fsck reported a problem",
            confidence=95, evidence=integrity, affected_components=[db_path, repo_dir], severity="critical",
        ))

    for report in reports:
        sysadmin_store.record_report(report)
    return reports
