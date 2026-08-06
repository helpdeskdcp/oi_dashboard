"""
test_agents/dev_agent/test_approval_engine.py -- regression tests for
agents/dev_agent/approval_engine.py's three-way decision.
"""
from agents.dev_agent import approval_engine as ae
from agents.dev_agent.gates.base import GateResult, GateStatus


def _gate(name, status, summary="ok"):
    return GateResult(gate=name, status=status, summary=summary)


class TestDecide:
    def test_self_modification_is_rejected_even_if_all_gates_would_pass(self):
        gates = [_gate("unit_tests", GateStatus.PASSED)]
        result = ae.decide(gates, self_modification_detected=True)
        assert result.decision == ae.Decision.REJECTED
        assert "agents/**" in result.reasoning

    def test_self_modification_is_rejected_with_no_gates_at_all(self):
        result = ae.decide([], self_modification_detected=True)
        assert result.decision == ae.Decision.REJECTED

    def test_any_failed_gate_rejects(self):
        gates = [
            _gate("unit_tests", GateStatus.PASSED),
            _gate("integration_tests", GateStatus.FAILED, summary="route test broke"),
        ]
        result = ae.decide(gates)
        assert result.decision == ae.Decision.REJECTED
        assert "route test broke" in result.reasoning

    def test_code_quality_skipped_requires_review_not_approval(self):
        gates = [
            _gate("unit_tests", GateStatus.PASSED),
            _gate("integration_tests", GateStatus.PASSED),
            _gate("benchmark", GateStatus.PASSED),
            _gate("code_quality", GateStatus.SKIPPED, summary="advisory-only"),
        ]
        result = ae.decide(gates)
        assert result.decision == ae.Decision.REQUIRES_REVIEW

    def test_backtest_skipped_for_structural_reasons_still_approves(self):
        gates = [
            _gate("unit_tests", GateStatus.PASSED),
            _gate("integration_tests", GateStatus.PASSED),
            _gate("backtest_compare", GateStatus.SKIPPED, summary="no strategy file touched"),
            _gate("benchmark", GateStatus.SKIPPED, summary="nothing to benchmark"),
            _gate("code_quality", GateStatus.PASSED),
        ]
        result = ae.decide(gates)
        assert result.decision == ae.Decision.APPROVED

    def test_all_gates_passed_approves(self):
        gates = [
            _gate("unit_tests", GateStatus.PASSED),
            _gate("integration_tests", GateStatus.PASSED),
            _gate("backtest_compare", GateStatus.PASSED),
            _gate("benchmark", GateStatus.PASSED),
            _gate("code_quality", GateStatus.PASSED),
        ]
        result = ae.decide(gates)
        assert result.decision == ae.Decision.APPROVED
        assert "zero regression" in result.reasoning

    def test_reasoning_mentions_every_gate(self):
        gates = [_gate("unit_tests", GateStatus.PASSED), _gate("integration_tests", GateStatus.PASSED)]
        result = ae.decide(gates)
        assert "unit_tests" in result.reasoning
        assert "integration_tests" in result.reasoning

    def test_result_carries_the_original_gate_results(self):
        gates = [_gate("unit_tests", GateStatus.PASSED)]
        result = ae.decide(gates)
        assert result.gate_results == gates
