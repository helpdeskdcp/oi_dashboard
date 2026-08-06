"""
agents/dev_agent/pipeline.py -- the orchestrator: worktree -> five gates
(unit tests, integration tests, backtest comparison, benchmark
comparison, code quality) -> approval decision -> audit trail. run()
validates a candidate branch that already has a committed change on it
(a human's change, or the base for a future agent). run_proposal() is
Milestone 3's addition: detect -> patch (both LLM-assisted, both go
through the same five gates below) -> approval -> audit trail, wiring
detector.py and patcher.py into the exact same validation path run()
already exercises, so an LLM-authored change is held to the identical
bar as a human-authored one.

Failure recovery: the first FAILED gate stops the run immediately, the
worktree is rolled back (agents.dev_agent.worktree.remove -- never
touches master or any other branch), and a complete audit row plus a
failure report is still written -- logs are preserved even though the
worktree isn't. A self-modification hit skips every gate entirely: the
plan's "refused before a worktree is even considered a candidate" rule
means gates never even run against a guarded diff. run_proposal() extends
this same recovery discipline one step earlier: a detection refusal never
creates a worktree at all, and a patch-generation failure (LLM error,
malformed response, or the LLM itself attempting a guarded write) is
rolled back by patcher.py before pipeline.py ever sees it -- either way,
run_proposal() still writes one complete audit row describing what
happened and why, exactly like a gate failure does.
"""
import dataclasses
from typing import Optional

from .. import audit_log
from .. import config
from . import detector, patcher, worktree, patch_generator, approval_engine
from .gates.base import GateResult, GateStatus
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


def run_proposal(repo_dir: str, trigger: str, target_files: list, *, base_ref: str = "main",
                  provider_name: Optional[str] = None, keep_worktree_on_success: bool = True) -> PipelineResult:
    """Milestone 3 entry point: detect -> patch -> the same five gates
    run() uses -> approval -> audit trail. Every requirement-10 field
    ("why it was generated, expected impact, risk assessment, test
    results, benchmark comparison, confidence score") lands in the audit
    row's payload["patch_report"] -- rationale/impact/risk/confidence
    come from patcher.py's LLM call, test_results/benchmark_comparison
    are pulled straight out of this same run's gate_results so they can
    never drift from what the gates actually reported."""
    detection = detector.detect(repo_dir, trigger, target_files, provider_name=provider_name)

    if detection.refused:
        row_id = audit_log.record(
            agent=AGENT_NAME, action_type="proposal",
            description=f"REJECTED: detection refused for trigger {trigger!r}",
            risk_tier="hard_blocked", outcome="rejected",
            payload={"trigger": trigger, "target_files": target_files, "reason": detection.refusal_reason},
        )
        return PipelineResult(
            decision=approval_engine.Decision.REJECTED, reasoning=detection.refusal_reason,
            gate_results=[], diff=None, changed_files=[], audit_log_id=row_id, worktree_removed=True,
        )

    try:
        wt, proposal = patcher.generate_patch(repo_dir, detection, base_ref=base_ref, provider_name=provider_name)
    except Exception as exc:
        # patcher.py has already rolled back any worktree it created --
        # nothing left to clean up here, only the audit trail to write.
        risk_tier = "hard_blocked" if isinstance(exc, patcher.SelfModificationRefused) else "needs_approval"
        failure_gate = GateResult(gate="patch_generation", status=GateStatus.FAILED, summary=str(exc))
        row_id = audit_log.record(
            agent=AGENT_NAME, action_type="proposal",
            description=f"REJECTED: patch generation failed for trigger {trigger!r}",
            risk_tier=risk_tier, outcome="rejected",
            payload={
                "trigger": trigger, "target_files": target_files,
                "detection": dataclasses.asdict(detection),
                "gates": [failure_gate.to_dict()],
                "error": str(exc),
            },
        )
        return PipelineResult(
            decision=approval_engine.Decision.REJECTED, reasoning=str(exc),
            gate_results=[failure_gate], diff=None, changed_files=[],
            audit_log_id=row_id, worktree_removed=True,
        )

    try:
        files = patch_generator.changed_files(wt.path, base_ref, wt.branch)
        self_mod = patch_generator.touches_guarded_path(files, config.SELF_MODIFICATION_GUARD_PREFIX)

        gate_results = [] if self_mod else _run_gates(wt.path, repo_dir, files)
        decision = approval_engine.decide(gate_results, self_modification_detected=self_mod)

        diff = patch_generator.generate(wt.path, base_ref, wt.branch)

        outcome = "rejected" if decision.decision == approval_engine.Decision.REJECTED else "pending_approval"
        risk_tier = "hard_blocked" if self_mod else "needs_approval"
        row_id = audit_log.record(
            agent=AGENT_NAME, action_type="proposal",
            description=f"{decision.decision.value}: {wt.branch} (LLM-generated via {proposal.provider_used})",
            risk_tier=risk_tier, outcome=outcome,
            payload={
                "branch": wt.branch, "base_ref": base_ref, "changed_files": files,
                "decision": decision.decision.value, "reasoning": decision.reasoning,
                "gates": [g.to_dict() for g in gate_results],
                "diff": diff,
                "detection": dataclasses.asdict(detection),
                "patch_report": {
                    "why": proposal.rationale,
                    "expected_impact": proposal.expected_impact,
                    "risk_assessment": proposal.risk_assessment,
                    "confidence_score": proposal.confidence_score,
                    "provider_used": proposal.provider_used,
                    "files_written": proposal.files_written,
                    "tests_written": proposal.tests_written,
                    "docs_written": proposal.docs_written,
                    "test_results": [
                        g.to_dict() for g in gate_results if g.gate in ("unit_tests", "integration_tests")
                    ],
                    "benchmark_comparison": next(
                        (g.to_dict() for g in gate_results if g.gate == "benchmark"), None
                    ),
                },
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
