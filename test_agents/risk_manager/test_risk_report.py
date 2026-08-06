"""
test_agents/risk_manager/test_risk_report.py -- regression tests for
RiskReport's JSON/human-readable rendering and its three constructors.
"""
import json

from agents.risk_manager import risk_engine, risk_report


def _assessment():
    return risk_engine.RiskAssessment(
        risk_score=72, decision="APPROVED",
        checks=[risk_engine.RiskCheckResult("position_sizing", True, 5.0, 1.0, "ok")],
        var=10.0, cvar=15.0, drawdown_simulation={"mean": 1.0, "percentile": 2.0, "worst": 3.0, "trials": 100},
        stress_test={"-0.5": {"net_pnl": -5.0, "max_drawdown": 5.0}},
        correlations={"other": 0.2}, explanation="Risk score 72/100 -> APPROVED.",
    )


class TestFromRiskAssessment:
    def test_carries_score_and_decision(self):
        report = risk_report.from_risk_assessment(_assessment(), subject="oi_delta_combo_NIFTY")
        assert report.report_type == "promotion"
        assert report.risk_score == 72
        assert report.decision == "APPROVED"
        assert report.details["var"] == 10.0
        assert report.details["checks"][0]["name"] == "position_sizing"

    def test_to_json_round_trips(self):
        report = risk_report.from_risk_assessment(_assessment(), subject="s")
        parsed = json.loads(report.to_json())
        assert parsed["risk_score"] == 72
        assert parsed["decision"] == "APPROVED"

    def test_human_readable_mentions_score_and_decision(self):
        report = risk_report.from_risk_assessment(_assessment(), subject="s")
        text = report.human_readable()
        assert "72/100" in text
        assert "APPROVED" in text


class TestFromPortfolioSnapshot:
    def test_has_no_risk_score_or_decision(self):
        report = risk_report.from_portfolio_snapshot({"summary": "all clear", "exposure": 1000})
        assert report.risk_score is None
        assert report.decision is None
        assert report.details["exposure"] == 1000


class TestFromAlert:
    def test_uses_metric_as_subject(self):
        report = risk_report.from_alert({"metric": "portfolio_heat", "message": "heat too high"})
        assert report.subject == "portfolio_heat"
        assert report.summary == "heat too high"
