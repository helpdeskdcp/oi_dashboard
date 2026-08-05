"""
agents/rollback.py -- non-destructive rollback of an applied agent
proposal. Always `git revert`, never `git reset --hard` -- see
AI_DEVELOPER_AGENT_PLAN.md's "Non-negotiables for this agent specifically"
section and this project's own git safety rules (never destructive
history rewrites).
"""
import subprocess

from . import audit_log


class RollbackError(Exception):
    """Raised for anything that prevents a clean, safe rollback -- an
    unknown audit_log id, a row that was never applied, a row with no
    recorded merge commit, or a failed `git revert`. Callers should treat
    this as "stop and show a human", not something to retry silently."""


def rollback(audit_log_id: int, *, repo_dir: str = ".", agent: str = "rollback") -> str:
    """Reverts the merge commit recorded against audit_log_id and records
    the rollback as its own audit_log row (never overwrites or deletes
    the original one). Returns git's stdout on success; raises
    RollbackError on any failure, including a failed `git revert` (e.g. a
    conflict) -- the failure is still logged before the exception
    propagates, so the attempt itself is never lost from the audit trail."""
    row = audit_log.get(audit_log_id)
    if row is None:
        raise RollbackError(f"no audit_log row with id={audit_log_id}")
    if row["outcome"] != "applied":
        raise RollbackError(
            f"row {audit_log_id} has outcome={row['outcome']!r}, expected 'applied' -- nothing to roll back"
        )
    merge_sha = row["merge_commit_sha"]
    if not merge_sha:
        raise RollbackError(f"row {audit_log_id} has no merge_commit_sha recorded -- cannot revert")

    result = subprocess.run(
        ["git", "revert", "--no-edit", merge_sha],
        cwd=repo_dir, capture_output=True, text=True,
    )
    succeeded = result.returncode == 0
    audit_log.record(
        agent=agent, action_type="rollback",
        description=(
            f"Rolled back audit_log id={audit_log_id} (merge {merge_sha[:8]})" if succeeded
            else f"Rollback FAILED for audit_log id={audit_log_id} (merge {merge_sha[:8]})"
        ),
        risk_tier="needs_approval", outcome="rolled_back" if succeeded else "failed",
        payload={
            "target_audit_log_id": audit_log_id, "merge_commit_sha": merge_sha,
            "git_returncode": result.returncode, "git_stdout": result.stdout, "git_stderr": result.stderr,
        },
    )
    if not succeeded:
        raise RollbackError(f"git revert failed for {merge_sha}: {result.stderr}")

    audit_log.set_outcome(audit_log_id, "rolled_back")
    return result.stdout
