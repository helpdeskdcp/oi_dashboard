"""
test_agents/trading_supervisor/test_supervision_report.py -- regression
tests for SupervisionReport and from_verdict().
"""
import json

from agents.trading_supervisor import supervision_engine, supervision_report


def _verdict(decision="APPROVED"):
    return supervision_engine.SupervisionVerdict(
        decision=decision, explanation="test explanation", risk_gate_status="passed",
        market_state={"symbol": "NIFTY"}, conflicts=[], data_health={"is_stale": False},
    )


class TestFromVerdict:
    def test_carries_decision_and_details(self):
        report = supervision_report.from_verdict(_verdict("REQUIRES_REVIEW"), subject="candidate")
        assert report.decision == "REQUIRES_REVIEW"
        assert report.summary == "test explanation"
        assert report.details["risk_gate_status"] == "passed"

    def test_to_json_round_trips(self):
        report = supervision_report.from_verdict(_verdict(), subject="candidate")
        parsed = json.loads(report.to_json())
        assert parsed["decision"] == "APPROVED"

    def test_human_readable_mentions_decision(self):
        report = supervision_report.from_verdict(_verdict("REJECTED"), subject="candidate")
        assert "REJECTED" in report.human_readable()
