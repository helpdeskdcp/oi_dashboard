"""
agents/runtime/approval_engine.py -- "Implement the missing approval
mechanism identified in the readiness report. Support: CLI approval,
Dashboard approval, Future API approval, Future mobile approval. Every
approval must be fully audited."

This is the #1 finding of AUTONOMOUS_READINESS_REPORT.md, closed: the
tool `approve_cli.py` is referenced by name in three module docstrings
(agents/audit_log.py, agents/dev_agent/approval_engine.py,
agents/quant_researcher/research_engine.py) since Milestone 1, but never
built until now. This module is the SHARED logic layer -- approve_cli.py
(repo root) is a thin CLI wrapper over it, so a dashboard route, a
future API endpoint, or a future mobile client can all call the exact
same functions and get the exact same audit trail, rather than each
channel growing its own copy of "what does approving something mean."

Two distinct kinds of "pending" this module resolves, never confused
with each other:
1. Code/strategy promotion proposals in agent_audit_log (outcome=
   'pending_approval', written by agents.dev_agent.pipeline.run_proposal()
   or agents.quant_researcher.research_engine's own _submit_for_approval())
   -- approving one of these can ACTUALLY MERGE real code (git merge
   --ff-only from the branch already recorded in the row's own payload),
   the one and only place in this entire framework where that happens,
   and it only ever happens because a human explicitly invoked this
   function. Still never auto-verifies with a re-run of the gates before
   merging (matches every manual milestone-merge in this project's own
   history: full-suite verification happens AFTER a fast-forward merge,
   not before) -- it DOES re-check fast-forward-mergeability right
   before merging, since time may have passed since the proposal was
   created.
2. Workflow-level approvals (agents.runtime.workflow_engine's own
   STAGE_HUMAN_APPROVAL checkpoint) -- never touches agent_audit_log at
   all, see workflow_engine.py's own docstring for why.
"""
import dataclasses
import subprocess

from .. import audit_log
from ..sys_admin import deployment_manager
from . import runtime_store, workflow_engine


@dataclasses.dataclass
class ApplyResult:
    audit_log_id: int
    merged: bool
    merge_commit_sha: str | None
    detail: str


def _run_git(args: list, *, repo_dir: str) -> str:
    """Same shape as agents.sys_admin.deployment_manager's own private
    _run_git -- kept as a separate copy rather than importing that
    module's underscore-prefixed internal, since apply_proposal() is the
    ONE place in this entire framework that performs a real `git merge`
    and deserves its own clearly-owned, non-borrowed implementation of
    exactly that one command."""
    result = subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def list_pending_proposals(*, agent: str | None = None) -> list:
    """Code/strategy proposals waiting on a human -- what approve_cli.py
    lists by default."""
    return audit_log.list_pending(agent=agent)


def approve_proposal(row_id: int, *, approved_by: str) -> dict:
    """Marks a proposal 'approved' -- does NOT merge it yet (matches
    agent_audit_log's own pending_approval -> approved -> applied state
    machine; apply_proposal() below performs the actual merge as a
    separate, explicit step)."""
    row = audit_log.get(row_id)
    if row is None:
        raise ValueError(f"no audit_log row with id={row_id}")
    if row["outcome"] != "pending_approval":
        raise ValueError(f"row {row_id} is not pending_approval (outcome={row['outcome']!r})")
    audit_log.set_outcome(row_id, "approved", approved_by=approved_by)
    return audit_log.get(row_id)


def reject_proposal(row_id: int, *, rejected_by: str) -> dict:
    row = audit_log.get(row_id)
    if row is None:
        raise ValueError(f"no audit_log row with id={row_id}")
    if row["outcome"] != "pending_approval":
        raise ValueError(f"row {row_id} is not pending_approval (outcome={row['outcome']!r})")
    audit_log.set_outcome(row_id, "rejected", approved_by=rejected_by)
    return audit_log.get(row_id)


def apply_proposal(row_id: int, *, applied_by: str, repo_dir: str = ".") -> ApplyResult:
    """Performs the actual git merge --ff-only for an ALREADY-approved
    code/strategy proposal, using the branch/base_ref agents.dev_agent.
    pipeline.run_proposal()/agents.quant_researcher.research_engine's
    _submit_for_approval() already recorded in the row's own payload --
    never a second source of truth for what to merge. Re-checks
    fast-forward-mergeability right before merging (reusing
    agents.sys_admin.deployment_manager, not a second git-mergeability
    check) since master may have moved since the proposal was created;
    refuses (raises, no merge attempted) if it no longer is one.
    Records the resulting commit SHA via agents.audit_log.set_outcome so
    agents/rollback.py can find it later -- that module already reads
    row['merge_commit_sha'] and would otherwise have nothing to revert."""
    row = audit_log.get(row_id)
    if row is None:
        raise ValueError(f"no audit_log row with id={row_id}")
    if row["outcome"] != "approved":
        raise ValueError(f"row {row_id} must be 'approved' before it can be applied (outcome={row['outcome']!r})")

    payload = row["payload_json"] or {}
    branch = payload.get("branch")
    base_ref = payload.get("base_ref", "main")
    if not branch:
        raise ValueError(f"row {row_id}'s payload has no 'branch' -- nothing to merge")

    if not deployment_manager.is_fast_forward_mergeable(branch, base_ref=base_ref, repo_dir=repo_dir):
        raise ValueError(
            f"branch {branch!r} is no longer fast-forward mergeable onto {base_ref!r} -- "
            f"{base_ref} has moved since this proposal was created; resolve manually before applying"
        )

    result = _run_git(["merge", branch, "--ff-only"], repo_dir=repo_dir)
    merge_sha = _run_git(["rev-parse", "HEAD"], repo_dir=repo_dir)

    audit_log.set_outcome(row_id, "applied", approved_by=applied_by, merge_commit_sha=merge_sha)
    return ApplyResult(audit_log_id=row_id, merged=True, merge_commit_sha=merge_sha, detail=result.strip())


# --- Workflow-level approvals (see module docstring, item 2) --------------

def list_pending_workflows() -> list:
    return runtime_store.list_workflows(status="waiting_approval")


def approve_workflow(workflow_id: int, *, approved_by: str, reason: str = "") -> dict:
    workflow_engine.approve_workflow(workflow_id, approved_by=approved_by, reason=reason)
    return runtime_store.get_workflow(workflow_id)


def reject_workflow(workflow_id: int, *, rejected_by: str, reason: str) -> dict:
    workflow_engine.reject_workflow(workflow_id, rejected_by=rejected_by, reason=reason)
    return runtime_store.get_workflow(workflow_id)
