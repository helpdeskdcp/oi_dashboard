"""
test_agents/risk_manager/test_api.py -- regression tests for the Risk
API's read/persist functions.
"""
from agents.risk_manager import api
from .conftest import insert_paper_order, insert_user


class TestGetPortfolioSnapshot:
    def test_returns_a_json_serializable_dict(self, paper_db, agent_db):
        insert_user(paper_db, 1, wallet_balance=100_000.0)
        result = api.get_portfolio_snapshot(user_id=1)
        assert result["report_type"] == "portfolio_snapshot"
        assert "exposure" in result["details"]

    def test_persists_snapshot_and_alerts(self, paper_db, agent_db, monkeypatch):
        from agents import config
        insert_user(paper_db, 1, wallet_balance=100.0)
        insert_paper_order(paper_db, user_id=1, entry_price=100.0, sl_price=0.0, qty=50, status="OPEN")
        monkeypatch.setattr(config, "RISK_PORTFOLIO_HEAT_LIMIT_PCT", 1.0)

        api.get_portfolio_snapshot(user_id=1)

        assert len(api.get_recent_snapshots(user_id=1)) == 1
        assert len(api.get_recent_alerts()) >= 1

    def test_persist_false_writes_nothing(self, paper_db, agent_db):
        insert_user(paper_db, 1, wallet_balance=100_000.0)
        api.get_portfolio_snapshot(user_id=1, persist=False)
        assert api.get_recent_snapshots(user_id=1) == []


class TestGetRecentAssessments:
    def test_delegates_to_risk_store(self, agent_db):
        from agents.risk_manager import risk_engine, risk_report, risk_store
        report = risk_report.from_risk_assessment(
            risk_engine.RiskAssessment(
                risk_score=80, decision="APPROVED", checks=[], var=0.0, cvar=0.0,
                drawdown_simulation={}, stress_test={}, correlations={}, explanation="ok",
            ),
            subject="s",
        )
        risk_store.record_assessment(report, candidate_name="s", symbol="NIFTY", strategy_family="f")
        assert len(api.get_recent_assessments(symbol="NIFTY")) == 1
