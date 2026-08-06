"""
agents/trading_supervisor/gate.py -- turns supervision_engine.verify()'s
SupervisionVerdict into a agents.dev_agent.gates.base.GateResult, so
Gate 7 composes with the SAME approval_engine.decide() every other gate
already goes through, rather than a second, parallel decision path.

Mapping (same conservative posture agents.risk_manager.gate already
established for Gate 6): REJECTED -> GateStatus.FAILED. APPROVED/
REQUIRES_REVIEW -> GateStatus.PASSED, with the full verdict (market
state, conflicts, data health, risk-gate cross-check) carried in
GateResult.details -- REQUIRES_REVIEW never silently disappears.

run() returns (GateResult, SupervisionVerdict, SupervisionReport), not
just a GateResult -- the caller (agents.quant_researcher.research_engine)
needs the fuller objects to persist via supervision_store.py, and
computing the verdict twice would risk it disagreeing with itself
(market_state.volatility_regime reads live-ish data that could change
between two calls).
"""
from ..dev_agent.gates.base import GateResult, GateStatus
from . import supervision_engine, supervision_report

GATE_NAME = "trading_supervision"


def run(*, candidate_name: str, symbol: str, direction: str, strategy_family: str,
        gate_results: list, memory_store, date: str | None = None,
        expiry_dates: set | None = None, event_dates: set | None = None):
    verdict = supervision_engine.verify(
        candidate_name=candidate_name, symbol=symbol, direction=direction, strategy_family=strategy_family,
        gate_results=gate_results, memory_store=memory_store, date=date,
        expiry_dates=expiry_dates, event_dates=event_dates,
    )
    report = supervision_report.from_verdict(verdict, subject=candidate_name)

    status = GateStatus.FAILED if verdict.decision == "REJECTED" else GateStatus.PASSED
    result = GateResult(
        gate=GATE_NAME, status=status, summary=verdict.explanation,
        details={"decision": verdict.decision, "report": report.to_dict()},
    )
    return result, verdict, report
