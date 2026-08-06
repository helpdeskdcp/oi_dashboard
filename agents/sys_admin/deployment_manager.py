"""
agents/sys_admin/deployment_manager.py -- "Build. Test. Benchmark. Merge
verification. Rollback. Version control. Safe deployment."

Build/Test/Benchmark reuse agents.dev_agent.pipeline.run_gates -- the
same gate sequence every other agent-authored change in this framework
already goes through, never a second implementation of what counts as
passing. Merge verification is dry-run ONLY: it reports whether a
branch is fast-forward-mergeable and whether it's green, but never runs
`git merge` itself -- consistent with this whole framework's "propose,
don't act" posture (and this session's own git policy: merges happen
only when a human asks for them). Rollback delegates entirely to the
existing agents/rollback.py (Milestone 1) -- not reimplemented here.
"""
import dataclasses
import subprocess

from .. import rollback as rollback_module
from ..dev_agent import patch_generator, worktree
from ..dev_agent.pipeline import run_gates


@dataclasses.dataclass
class DeploymentReport:
    branch: str
    ff_mergeable: bool
    gate_results: list
    all_gates_passed: bool
    reason: str


def _run_git(args: list, *, repo_dir: str = ".") -> str:
    result = subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def current_version(*, repo_dir: str = ".") -> dict:
    """Real git-based version info -- never a hand-maintained VERSION
    file that could drift out of sync with what's actually deployed."""
    try:
        return {
            "commit": _run_git(["rev-parse", "HEAD"], repo_dir=repo_dir),
            "describe": _run_git(["describe", "--tags", "--always"], repo_dir=repo_dir),
            "branch": _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_dir=repo_dir),
        }
    except RuntimeError as exc:
        return {"commit": None, "describe": None, "branch": None, "error": str(exc)}


def is_fast_forward_mergeable(branch: str, *, base_ref: str = "HEAD", repo_dir: str = ".") -> bool:
    """True iff base_ref is an ancestor of branch's tip -- i.e. merging
    branch into base_ref would be a pure fast-forward, the ONLY merge
    kind this framework ever performs (see every prior milestone's own
    merge report)."""
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_ref, branch], cwd=repo_dir, capture_output=True, text=True,
    )
    return result.returncode == 0


def verify_merge_readiness(branch: str, *, base_ref: str = "HEAD", repo_dir: str = ".") -> DeploymentReport:
    """Build + Test + Benchmark + merge-check, all real, all read-only.
    Checks the candidate branch out into an isolated worktree and runs
    the same run_gates() sequence every other proposal in this
    framework goes through -- never merges, never pushes."""
    ff_ok = is_fast_forward_mergeable(branch, base_ref=base_ref, repo_dir=repo_dir)
    wt = worktree.checkout_existing(branch, repo_dir=repo_dir)
    try:
        files = patch_generator.changed_files(wt.path, base_ref, branch)
        gate_results = run_gates(wt.path, repo_dir, files)
    finally:
        worktree.remove(wt)

    all_passed = all(g.passed for g in gate_results)
    if not ff_ok:
        reason = "NOT fast-forward mergeable"
    elif not all_passed:
        failed = sum(1 for g in gate_results if not g.passed)
        reason = f"{failed} gate(s) failed"
    else:
        reason = "fast-forward mergeable and all gates passed"

    return DeploymentReport(
        branch=branch, ff_mergeable=ff_ok, gate_results=gate_results, all_gates_passed=all_passed, reason=reason,
    )


def rollback(audit_log_id: int, *, repo_dir: str = ".") -> str:
    """Delegates entirely to agents.rollback.rollback -- git revert
    only, never a destructive reset, exactly as that module's own
    docstring already requires."""
    return rollback_module.rollback(audit_log_id, repo_dir=repo_dir)
