"""
test_agents/trading_supervisor/test_supervision_store.py -- regression
tests for supervision_store.py's SQLite persistence. Uses the shared
agent_db fixture (test_agents/conftest.py), which already points
supervision_store.DB_PATH at a throwaway file and calls init_db().
"""
from agents.trading_supervisor import supervision_report, supervision_store


def _report(decision="APPROVED"):
    return supervision_report.SupervisionReport(
        subject="test_strategy", decision=decision, summary="test summary", details={"x": 1},
    )


class TestInitDb:
    def test_is_idempotent(self, agent_db):
        supervision_store.init_db()
        supervision_store.init_db()


class TestRecordAndListSupervision:
    def test_round_trip(self, agent_db):
        row_id = supervision_store.record_supervision(
            _report("APPROVED"), candidate_name="test_strategy", symbol="NIFTY", audit_log_id=5,
        )
        assert row_id == 1
        hits = supervision_store.list_supervision_log(symbol="NIFTY")
        assert len(hits) == 1
        assert hits[0]["decision"] == "APPROVED"
        assert hits[0]["report_json"]["summary"] == "test summary"

    def test_filters_by_decision(self, agent_db):
        supervision_store.record_supervision(_report("APPROVED"), candidate_name="a", symbol="NIFTY")
        supervision_store.record_supervision(_report("REJECTED"), candidate_name="b", symbol="NIFTY")
        assert len(supervision_store.list_supervision_log(decision="REJECTED")) == 1

    def test_orders_most_recent_first(self, agent_db):
        supervision_store.record_supervision(_report(), candidate_name="first", symbol="NIFTY")
        supervision_store.record_supervision(_report(), candidate_name="second", symbol="NIFTY")
        hits = supervision_store.list_supervision_log(symbol="NIFTY")
        assert [h["candidate_name"] for h in hits] == ["second", "first"]


class TestRecordAndListAgentHealth:
    def test_round_trip(self, agent_db):
        row_id = supervision_store.record_agent_health(
            "dev_agent", is_stale=False, is_failing=True, snapshot={"recent_activity_count": 5},
        )
        assert row_id == 1
        hits = supervision_store.list_agent_health(agent="dev_agent")
        assert len(hits) == 1
        assert hits[0]["is_failing"] == 1
        assert hits[0]["snapshot_json"]["recent_activity_count"] == 5

    def test_filters_by_agent(self, agent_db):
        supervision_store.record_agent_health("dev_agent", is_stale=False, is_failing=False, snapshot={})
        supervision_store.record_agent_health("risk_manager", is_stale=False, is_failing=False, snapshot={})
        assert len(supervision_store.list_agent_health(agent="risk_manager")) == 1
