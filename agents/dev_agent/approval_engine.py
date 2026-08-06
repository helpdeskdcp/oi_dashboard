"""
agents/dev_agent/approval_engine.py -- turns a set of GateResults into
exactly one decision: APPROVED, REJECTED, or REQUIRES_REVIEW, each with a
full plain-text reasoning trail. This module never merges or applies
anything -- like BaseAgent itself (no apply/execute method anywhere in
the framework), it only classifies. Even an APPROVED decision here only
ever feeds BaseAgent.propose(), which writes "pending_approval" to the
audit log; a human still acts via approve_cli.py (Milestone 4).
"""
import dataclasses
import enum

from .gates.base import GateResult, GateStatus


class Decision(str, enum.Enum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REQUIRES_REVIEW = "REQUIRES_REVIEW"


@dataclasses.dataclass
class ApprovalResult:
    decision: Decision
    reasoning: str
    gate_results: list


def decide(gate_results: list, *, self_modification_detected: bool = False) -> ApprovalResult:
    """
    - self_modification_detected -> REJECTED unconditionally, before any
      gate result even matters (hard block, not a softer tier -- matches
      agents.config.SELF_MODIFICATION_GUARD_PREFIX).
    - Any gate FAILED -> REJECTED. The five gates are a strict AND gate;
      the explicit instruction ("reject any change that decreases
      profitability, increases drawdown, reduces stability, or causes
      test failures") means a benchmark regression is a hard REJECTED,
      not a softer REQUIRES_REVIEW.
    - All gates PASSED/SKIPPED, but code_quality was SKIPPED because no
      scanning tool was available -> REQUIRES_REVIEW: the agent could not
      independently confirm maintainability/security on this patch, so a
      human should look at it even though nothing failed outright.
    - All gates PASSED (SKIPPED only for structural reasons, e.g.
      backtest_compare on a diff that touched no strategy file) ->
      APPROVED.
    """
    reasons = []

    if self_modification_detected:
        reasons.append(
            "diff touches a self-modification-guarded path (agents/**) -- "
            "hard rejected, no override exists for this check."
        )
        return ApprovalResult(Decision.REJECTED, "\n".join(reasons), gate_results)

    for g in gate_results:
        reasons.append(f"[{g.gate}] {g.status.value.upper()}: {g.summary}")

    failed = [g for g in gate_results if g.status == GateStatus.FAILED]
    if failed:
        return ApprovalResult(Decision.REJECTED, "\n".join(reasons), gate_results)

    quality_advisory = any(
        g.status == GateStatus.SKIPPED and g.gate == "code_quality" for g in gate_results
    )
    if quality_advisory:
        reasons.append(
            "code_quality ran in advisory-only mode (no lint/type/security tooling "
            "installed) -- a human should review maintainability and security manually."
        )
        return ApprovalResult(Decision.REQUIRES_REVIEW, "\n".join(reasons), gate_results)

    reasons.append("all gates passed with zero regression across every tracked metric.")
    return ApprovalResult(Decision.APPROVED, "\n".join(reasons), gate_results)
