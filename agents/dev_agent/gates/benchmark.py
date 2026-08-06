"""
agents/dev_agent/gates/benchmark.py -- Gate 4. Consumes Gate 3's
(backtest_compare) raw comparison output rather than re-running the
backtest -- that's the expensive step, and duplicating it here would
buy nothing. This is the gate whose FAILED status is the actual
performance-regression block; backtest_compare (Gate 3) can be SKIPPED
(no strategy file touched), in which case this gate is SKIPPED too --
there is nothing to benchmark against.
"""
from .base import GateResult, GateStatus

GATE_NAME = "benchmark"


def run(backtest_gate_result) -> GateResult:
    if backtest_gate_result.status == GateStatus.SKIPPED:
        return GateResult(
            gate=GATE_NAME, status=GateStatus.SKIPPED,
            summary="backtest comparison was skipped -- nothing to benchmark",
            details={},
        )
    if backtest_gate_result.status == GateStatus.FAILED and "comparison" not in backtest_gate_result.details:
        return GateResult(
            gate=GATE_NAME, status=GateStatus.FAILED,
            summary="backtest comparison could not be run -- benchmark gate cannot proceed",
            details={},
        )

    comparison = backtest_gate_result.details.get("comparison", {})
    regressions = comparison.get("regressions", [])
    if regressions:
        names = ", ".join(r["metric"] for r in regressions)
        return GateResult(
            gate=GATE_NAME, status=GateStatus.FAILED,
            summary=f"benchmark regression on: {names}",
            details={"comparison": comparison},
        )
    return GateResult(
        gate=GATE_NAME, status=GateStatus.PASSED,
        summary="candidate matches or beats baseline on every tracked metric",
        details={"comparison": comparison},
    )
