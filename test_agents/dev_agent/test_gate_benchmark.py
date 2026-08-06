"""
test_agents/dev_agent/test_gate_benchmark.py -- Gate 4. Consumes a
synthetic backtest_compare GateResult rather than running Gate 3 for
real, since benchmark.run()'s only job is to read Gate 3's comparison
table and render the final pass/fail.
"""
from agents.dev_agent.gates import benchmark
from agents.dev_agent.gates.base import GateResult, GateStatus


def _backtest_result(status, comparison=None):
    return GateResult(gate="backtest_compare", status=status, summary="synthetic",
                       details={"comparison": comparison} if comparison is not None else {})


class TestBenchmarkGate:
    def test_skipped_when_backtest_gate_skipped(self):
        bt = _backtest_result(GateStatus.SKIPPED)
        result = benchmark.run(bt)
        assert result.status == GateStatus.SKIPPED

    def test_failed_when_backtest_gate_could_not_run(self):
        bt = _backtest_result(GateStatus.FAILED)  # no "comparison" key at all
        result = benchmark.run(bt)
        assert result.status == GateStatus.FAILED

    def test_passed_when_no_regressions_in_comparison(self):
        bt = _backtest_result(GateStatus.PASSED, comparison={"metrics": [], "regressions": []})
        result = benchmark.run(bt)
        assert result.status == GateStatus.PASSED

    def test_failed_when_comparison_has_regressions(self):
        regressions = [{"metric": "net_pnl", "baseline": 100, "candidate": 50}]
        bt = _backtest_result(GateStatus.FAILED, comparison={"metrics": [], "regressions": regressions})
        result = benchmark.run(bt)
        assert result.status == GateStatus.FAILED
        assert "net_pnl" in result.summary
