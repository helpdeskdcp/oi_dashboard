"""
agents/risk_manager/gate.py -- turns risk_intelligence.assess()'s
RiskAssessment into a agents.dev_agent.gates.base.GateResult, so the
Promotion Risk Gate composes with the SAME approval_engine.decide()
every other gate already goes through, rather than a second, parallel
decision path.

Mapping (deliberately conservative): a REJECTED risk assessment becomes
GateStatus.FAILED -- approval_engine.decide()'s existing "any FAILED
gate -> REJECTED" rule then hard-blocks the promotion, exactly like a
failing backtest_compare/code_quality gate already does today. Both
APPROVED and REQUIRES_REVIEW become GateStatus.PASSED -- REQUIRES_REVIEW
never silently disappears, it's carried in full (risk_score, decision,
every individual check, VaR/CVaR/drawdown-sim/stress-test/correlations)
inside GateResult.details, where a human reviewing the pending_approval
row sees it plainly. Nothing about approval_engine.py itself changes.

run() returns (GateResult, RiskAssessment, RiskReport) rather than just
a GateResult -- one call computes the assessment (drawdown simulation is
randomized, so calling it twice could disagree with itself), and the
caller (agents.quant_researcher.research_engine) needs the fuller
objects to persist via risk_store.py and fold into the audit trail, not
just the gate's pass/fail.
"""
from ..dev_agent.gates.base import GateResult, GateStatus
from . import risk_intelligence, risk_report

GATE_NAME = "risk_assessment"


def run(*, candidate_name: str, symbol: str, strategy_family: str, stop_points: float,
        trades: list, candidate_thresholds: dict, memory_store, capital: float | None = None):
    assessment = risk_intelligence.assess(
        memory_store, candidate_name=candidate_name, symbol=symbol, strategy_family=strategy_family,
        stop_points=stop_points, trades=trades, candidate_thresholds=candidate_thresholds, capital=capital,
    )
    report = risk_report.from_risk_assessment(assessment, subject=candidate_name)

    status = GateStatus.FAILED if assessment.decision == "REJECTED" else GateStatus.PASSED
    result = GateResult(
        gate=GATE_NAME, status=status, summary=assessment.explanation,
        details={"risk_score": assessment.risk_score, "decision": assessment.decision, "report": report.to_dict()},
    )
    return result, assessment, report
