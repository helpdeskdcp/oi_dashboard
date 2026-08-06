"""
agents/dev_agent/pipeline.py -- Milestone 2's orchestrator: worktree ->
five gates (unit tests, integration tests, backtest comparison,
benchmark comparison, code quality) -> approval decision -> audit trail.
No LLM call lives here yet (that's Milestone 3) -- run() validates a
candidate branch that ALREADY has a committed change on it, exactly the
way a real proposal will be validated once the detector/patcher land.

Failure recovery: the first FAILED gate stops the run immediately, the
worktree is rolled back (agents.dev_agent.worktree.remove -- never
touches master or any other branch), and a complete audit row plus a
failure report is still written -- logs are preserved even though the
worktree isn't. A self-modification hit skips every gate entirely: the
plan's "refused before a worktree is even considered a candidate" rule
means gates never even run against a guarded diff.
"""
import dataclasses
from typing import Optional

from .. import audit_log
from .. import config
from . import worktree, patch_generator, approval_engine
from .gates.base import GateStatus
from .gates import unit_tests, integration_tests, backtest_compare, benchmark, code_quality

AGENT_NAME = "dev_agent"


@dataclasses.dataclass
class PipelineResult:
    decision: approval_engine.Decision
    reasoning: str
    gate_results: list
    diff: Optional[str]
    changed_files: list
    audit_log_id: int
    worktree_removed: bool


def _run_gates(wt_path, repo_dir, files):
    """Runs gates 1-5 in the fixed order, short-circuiting at the first
    FAILED result -- "any failure ends the run immediately" -- while
    still always running SKIPPED-eligible gates 3/4 in sequence so the
    benchmark gate has backtest_compare's result to consume."""
    results = []

    results.append(unit_tests.run(wt_path))
    if results[-1].status == GateStatus.FAILED:
        return results

    results.append(integration_tests.run(wt_path))
    if results[-1].status == GateStatus.FAILED:
        return results

    bt_result = backtest_compare.run(repo_dir, wt_path, files)
    results.append(bt_result)
    if bt_result.status == GateStatus.FAILED:
        results.append(benchmark.run(bt_result))
        return results

    results.append(benchmark.run(bt_result))
    if results[-1].status == GateStatus.FAILED:
        return results

    results.append(code_quality.run(wt_path))
    return results


def run(candidate_branch: str, *, repo_dir: str = ".", base_ref: str = "main",
        keep_worktree_on_success: bool = True) -> PipelineResult:
    wt = worktree.checkout_existing(candidate_branch, repo_dir=repo_dir)
    try:
        files = patch_generator.changed_files(wt.path, base_ref, candidate_branch)
        self_mod = patch_generator.touches_guarded_path(files, config.SELF_MODIFICATION_GUARD_PREFIX)

        gate_results = [] if self_mod else _run_gates(wt.path, repo_dir, files)
        decision = approval_engine.decide(gate_results, self_modification_detected=self_mod)

        diff = patch_generator.generate(wt.path, base_ref, candidate_branch)

        outcome = "rejected" if decision.decision == approval_engine.Decision.REJECTED else "pending_approval"
        risk_tier = "hard_blocked" if self_mod else "needs_approval"
        row_id = audit_log.record(
            agent=AGENT_NAME, action_type="proposal",
            description=f"{decision.decision.value}: {candidate_branch}",
            risk_tier=risk_tier, outcome=outcome,
            payload={
                "branch": candidate_branch, "base_ref": base_ref, "changed_files": files,
                "decision": decision.decision.value, "reasoning": decision.reasoning,
                "gates": [g.to_dict() for g in gate_results],
                "diff": diff,
            },
        )

        should_remove = decision.decision == approval_engine.Decision.REJECTED or not keep_worktree_on_success
        removed = False
        if should_remove:
            worktree.remove(wt)
            removed = True

        return PipelineResult(
            decision=decision.decision, reasoning=decision.reasoning,
            gate_results=gate_results, diff=diff, changed_files=files,
            audit_log_id=row_id, worktree_removed=removed,
        )
    except Exception:
        worktree.remove(wt)
        raise
