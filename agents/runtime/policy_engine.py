"""
agents/runtime/policy_engine.py -- "Support runtime policies... Policies
must be configurable without changing code."

Seven policies, exactly as specified. Configurable via
config.RUNTIME_DEFAULT_POLICY (an env var, no code change) or a live
override written to runtime_store's runtime_policy table (via
approve_cli.py or the dashboard, no restart needed) -- the override
always wins once set.

CRITICAL SAFETY SCOPING, read before touching this file: this policy
taxonomy governs how far agents/runtime/workflow_engine.py is allowed to
ADVANCE a workflow (specifically: whether it may proceed past the Human
Approval stage without a human actually clicking approve). It does NOT
and CANNOT unlock a live broker order, under ANY policy including
"full_auto" -- there is no code path anywhere in agents/ that calls a
live broker API (a hard invariant since Milestone 1, verified
programmatically by security_audit.check_propose_only_invariant() and,
this sprint, by an AST scan for the live SDK's own class names), and the
Execution workflow stage (workflow_engine.py) only ever writes a
structured, position-sized RECOMMENDATION record -- reusing
position_sizing.compute_quantity, the same pure-math sizing every other
agent already trusts -- never a call into app.py's own paper-trade
tables (importing app.py from agents/ is the same landmine this whole
framework has refused since Milestone 6: a ~7000-line Flask app with
real broker-session machinery). "full_auto" therefore means "advances
without waiting for a human at the approval checkpoint," not "places
real or even paper orders unattended" -- see AI_RUNTIME.md's own
"Policy Engine scoping" section for the full reasoning. Widening this
later needs its own dedicated, adversarial test suite proving no data/
capital loss is possible, the same standard self_healing.py's own
automatic-recovery lane was held to in Milestone 8 -- never a quiet
scope creep here.
"""
from .. import config
from ..sys_admin import sysadmin_report, sysadmin_store
from . import runtime_store

READ_ONLY = "read_only"
RECOMMENDATION_ONLY = "recommendation_only"
SIMULATION = "simulation"
PAPER_TRADING = "paper_trading"
SEMI_AUTO = "semi_auto"
FULL_AUTO = "full_auto"
EMERGENCY_STOP = "emergency_stop"

ALL_POLICIES = (
    READ_ONLY, RECOMMENDATION_ONLY, SIMULATION, PAPER_TRADING, SEMI_AUTO, FULL_AUTO, EMERGENCY_STOP,
)

# What each policy actually permits -- consulted by workflow_engine.py.
# advances_workflow: may a NEW workflow be started at all?
# reaches_execution: may the workflow proceed past Supervisor to a
#   recommendation at all, or does it stop as pure observation?
# auto_approve: may it skip the Human Approval checkpoint (still only
#   ever produces a RECOMMENDATION at Execution -- see module docstring)?
_RULES = {
    READ_ONLY:            {"advances_workflow": False, "reaches_execution": False, "auto_approve": False},
    RECOMMENDATION_ONLY:  {"advances_workflow": True,  "reaches_execution": False, "auto_approve": False},
    SIMULATION:           {"advances_workflow": True,  "reaches_execution": True,  "auto_approve": False},
    PAPER_TRADING:        {"advances_workflow": True,  "reaches_execution": True,  "auto_approve": False},
    SEMI_AUTO:            {"advances_workflow": True,  "reaches_execution": True,  "auto_approve": False},
    FULL_AUTO:            {"advances_workflow": True,  "reaches_execution": True,  "auto_approve": True},
    EMERGENCY_STOP:       {"advances_workflow": False, "reaches_execution": False, "auto_approve": False},
}


def get_active_policy() -> str:
    override = runtime_store.get_policy_override()
    if override is not None:
        return override["policy"]
    return config.RUNTIME_DEFAULT_POLICY


def set_policy(policy: str, *, changed_by: str, reason: str):
    """Records who/why, always -- a policy change is exactly the kind of
    autonomous-adjacent action this framework logs with full
    explainability (reusing agents.sys_admin.sysadmin_report's shared
    shape rather than a new, fourth parallel report dataclass -- see
    AUTONOMOUS_READINESS_REPORT.md's own flagged "three parallel report
    dataclasses" debt item, which this milestone deliberately avoids
    making worse)."""
    if policy not in ALL_POLICIES:
        raise ValueError(f"policy must be one of {ALL_POLICIES}, got {policy!r}")
    runtime_store.set_policy_override(policy, changed_by=changed_by, reason=reason)
    report = sysadmin_report.build(
        module="policy_engine", action="set_policy",
        reason=f"policy changed to {policy!r} by {changed_by}: {reason}",
        confidence=100, evidence={"policy": policy, "changed_by": changed_by, "reason": reason},
        severity="warning" if policy == EMERGENCY_STOP else "info",
    )
    sysadmin_store.record_report(report)
    return report


def rules_for(policy: str | None = None) -> dict:
    policy = policy or get_active_policy()
    if policy not in _RULES:
        raise ValueError(f"policy must be one of {ALL_POLICIES}, got {policy!r}")
    return _RULES[policy]


def can_start_workflow() -> bool:
    return rules_for()["advances_workflow"]


def can_reach_execution() -> bool:
    return rules_for()["reaches_execution"]


def auto_approves() -> bool:
    return rules_for()["auto_approve"]


def is_emergency_stop() -> bool:
    return get_active_policy() == EMERGENCY_STOP
