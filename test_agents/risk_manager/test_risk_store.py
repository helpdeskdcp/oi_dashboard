"""
test_agents/risk_manager/test_risk_store.py -- regression tests for
agents/risk_manager/risk_store.py's SQLite persistence. Uses the shared
agent_db fixture (test_agents/conftest.py), which already points
risk_store.DB_PATH at a throwaway file and calls risk_store.init_db().
"""
from agents.risk_manager import risk_engine, risk_report, risk_store


def _promotion_report(decision="APPROVED", score=80):
    assessment = risk_engine.RiskAssessment(
        risk_score=score, decision=decision, checks=[], var=0.0, cvar=0.0,
        drawdown_simulation={}, stress_test={}, correlations={}, explanation="test",
    )
    return risk_report.from_risk_assessment(assessment, subject="test_strategy")


class TestInitDb:
    def test_is_idempotent(self, agent_db):
        risk_store.init_db()
        risk_store.init_db()  # must not raise


class TestRecordAndListAssessments:
    def test_round_trip(self, agent_db):
        report = _promotion_report(decision="APPROVED", score=85)
        row_id = risk_store.record_assessment(
            report, candidate_name="test_strategy", symbol="NIFTY",
            strategy_family="oi_delta_combo", audit_log_id=7,
        )
        assert row_id == 1
        hits = risk_store.list_assessments(symbol="NIFTY")
        assert len(hits) == 1
        assert hits[0]["decision"] == "APPROVED"
        assert hits[0]["risk_score"] == 85
        assert hits[0]["report_json"]["subject"] == "test_strategy"

    def test_filters_by_decision(self, agent_db):
        risk_store.record_assessment(
            _promotion_report(decision="APPROVED"), candidate_name="a", symbol="NIFTY", strategy_family="f",
        )
        risk_store.record_assessment(
            _promotion_report(decision="REJECTED"), candidate_name="b", symbol="NIFTY", strategy_family="f",
        )
        assert len(risk_store.list_assessments(decision="REJECTED")) == 1

    def test_orders_most_recent_first(self, agent_db):
        risk_store.record_assessment(_promotion_report(), candidate_name="first", symbol="NIFTY", strategy_family="f")
        risk_store.record_assessment(_promotion_report(), candidate_name="second", symbol="NIFTY", strategy_family="f")
        hits = risk_store.list_assessments(symbol="NIFTY")
        assert [h["candidate_name"] for h in hits] == ["second", "first"]


class TestRecordAndListAlerts:
    def test_round_trip(self, agent_db):
        report = risk_report.from_alert({"metric": "portfolio_heat", "message": "too hot"})
        row_id = risk_store.record_alert(
            report, metric="portfolio_heat", severity="critical", value=9.0, limit_value=6.0, user_id=3,
            recommendation="reduce position size",
        )
        assert row_id == 1
        hits = risk_store.list_alerts(severity="critical")
        assert len(hits) == 1
        assert hits[0]["metric"] == "portfolio_heat"
        assert hits[0]["recommendation"] == "reduce position size"

    def test_filters_by_severity(self, agent_db):
        report = risk_report.from_alert({"metric": "x", "message": "y"})
        risk_store.record_alert(report, metric="x", severity="warning")
        risk_store.record_alert(report, metric="x", severity="critical")
        assert len(risk_store.list_alerts(severity="critical")) == 1

    def test_filters_by_user_id(self, agent_db):
        report = risk_report.from_alert({"metric": "x", "message": "y"})
        risk_store.record_alert(report, metric="x", severity="warning", user_id=1)
        risk_store.record_alert(report, metric="x", severity="warning", user_id=2)
        assert len(risk_store.list_alerts(user_id=1)) == 1


class TestRecordAndListSnapshots:
    def test_round_trip(self, agent_db):
        report = risk_report.from_portfolio_snapshot({"summary": "ok", "exposure": 1000})
        row_id = risk_store.record_snapshot(
            report, user_id=1, exposure=1000.0, portfolio_heat=2.5,
            margin_utilization=40.0, daily_pnl=-50.0, max_drawdown=100.0,
        )
        assert row_id == 1
        hits = risk_store.list_snapshots(user_id=1)
        assert len(hits) == 1
        assert hits[0]["exposure"] == 1000.0
        assert hits[0]["snapshot_json"]["details"]["exposure"] == 1000

    def test_filters_by_user(self, agent_db):
        report = risk_report.from_portfolio_snapshot({"summary": "ok"})
        risk_store.record_snapshot(report, user_id=1)
        risk_store.record_snapshot(report, user_id=2)
        assert len(risk_store.list_snapshots(user_id=1)) == 1
