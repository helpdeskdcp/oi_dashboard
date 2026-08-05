"""
agents/base_agent.py -- the shared interface every BATI autonomous agent
implements (RiskTier, Finding, ProposedAction, BaseAgent). Milestone 1 of
AI_DEVELOPER_AGENT_PLAN.md -- agents/dev_agent/ (Milestone 2+) is the
first concrete BaseAgent subclass; this module has no agent-specific
logic in it at all, so a future System Administrator agent plugs into
the exact same three classes without anything here changing.
"""
from __future__ import annotations

import dataclasses
import enum
from typing import Any, Optional

from . import audit_log as _audit_log_module

_VALID_SEVERITIES = ("info", "warning", "critical")


class RiskTier(str, enum.Enum):
    """See AUTONOMOUS_AGENTS_ARCHITECTURE.md's "The mechanism" section --
    every agent action resolves to exactly one of these three lanes.
    Subclassing str means the enum value serializes into JSON/SQLite as
    the plain string ("read_only", not "RiskTier.READ_ONLY"), so a row
    written today reads back identically after any future refactor."""
    READ_ONLY = "read_only"
    NEEDS_APPROVAL = "needs_approval"
    HARD_BLOCKED = "hard_blocked"


@dataclasses.dataclass
class ProposedAction:
    """A change an agent wants made. Constructing one does nothing by
    itself -- only BaseAgent.propose() ever turns it into an audit_log
    row, and even then never executes it. risk_tier=HARD_BLOCKED is only
    ever constructed to RECORD a refusal (see agents/dev_agent/detector.py's
    agents/** guard, Milestone 3) -- propose() logs it as already
    "rejected", since there is nothing to approve."""
    risk_tier: RiskTier
    description: str
    diff: Optional[str] = None
    test_results: Optional[dict[str, Any]] = None
    integration_test_results: Optional[dict[str, Any]] = None
    backtest_comparison: Optional[dict[str, Any]] = None
    benchmark_comparison: Optional[dict[str, Any]] = None


@dataclasses.dataclass
class Finding:
    """One observation from an agent's run_cycle()/on_event(). severity is
    independent of risk_tier: a critical FINDING (e.g. "tests are failing
    on master right now") can carry no ProposedAction at all if the agent
    has nothing to suggest yet -- the two fields answer different
    questions ("how urgent is this" vs "what, if anything, should change").

    evidence is required and must be non-empty -- per the architecture
    doc's safety layer ("no evidence, no Finding"), a claim without a log
    line, test result, or query result attached is a bug in the agent
    that produced it, not a valid Finding."""
    severity: str
    summary: str
    evidence: dict[str, Any]
    proposed_action: Optional[ProposedAction] = None

    def __post_init__(self):
        if self.severity not in _VALID_SEVERITIES:
            raise ValueError(f"severity must be one of {_VALID_SEVERITIES}, got {self.severity!r}")
        if not self.evidence:
            raise ValueError("Finding.evidence must be non-empty -- a Finding with no evidence is a bug, not a finding")


class BaseAgent:
    """Every agent subclasses this and implements run_cycle() and/or
    on_event(). propose() is the ONLY sanctioned way for a subclass to
    record a NEEDS_APPROVAL action -- there is deliberately no method
    anywhere on this class that applies, merges, or executes a change."""

    name: str = "base"

    def __init__(self, audit_log_module=None):
        # Dependency-injectable for tests (test_agents/ can pass a module
        # pointed at a throwaway DB); defaults to the real module so
        # production callers don't have to know this parameter exists.
        self._audit_log = audit_log_module or _audit_log_module

    def run_cycle(self) -> list[Finding]:
        """Scheduled/triggered entry point -- see AI_DEVELOPER_AGENT_PLAN.md's
        "Execution Cadence" decision (trigger-driven, not a timer). Must be
        overridden; the base implementation refuses to silently no-op."""
        raise NotImplementedError(f"{type(self).__name__} must implement run_cycle()")

    def on_event(self, event: dict) -> None:
        """Reactive entry point for events published by OTHER agents via
        agents/event_bus.py. Optional -- the base implementation is a
        no-op, since a single-agent deployment (Milestone 1-4) has nothing
        to react to yet."""

    def propose(self, action: ProposedAction, *, agent_name: Optional[str] = None) -> int:
        """Writes exactly one agent_audit_log row for `action` and returns
        its id. HARD_BLOCKED actions are logged with outcome="rejected"
        immediately -- there is nothing to approve, so they never enter
        the pending_approval queue. Every other risk tier is logged as
        outcome="pending_approval" -- there is no parameter or config
        value on this method that skips that, matching the "human
        approval required for code changes by default" requirement."""
        name = agent_name or self.name
        payload = {
            "diff": action.diff,
            "test_results": action.test_results,
            "integration_test_results": action.integration_test_results,
            "backtest_comparison": action.backtest_comparison,
            "benchmark_comparison": action.benchmark_comparison,
        }
        if action.risk_tier == RiskTier.HARD_BLOCKED:
            return self._audit_log.record(
                agent=name, action_type="proposal", description=action.description,
                risk_tier=action.risk_tier.value, outcome="rejected",
                payload={**payload, "reason": "hard_blocked -- refused before a worktree was ever created"},
            )
        return self._audit_log.record(
            agent=name, action_type="proposal", description=action.description,
            risk_tier=action.risk_tier.value, outcome="pending_approval",
            payload=payload,
        )
