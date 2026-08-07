from agents.runtime import api as runtime_api
from agents.runtime import runtime_store as rs


class TestGetRuntimeOverview:
    def test_returns_every_documented_section(self, agent_db):
        overview = runtime_api.get_runtime_overview(db_path=agent_db)
        assert set(overview.keys()) == {
            "policy", "market", "agents", "queue", "workflows_running",
            "workflows_waiting_approval", "recent_events", "infrastructure",
        }

    def test_reflects_a_running_workflow(self, agent_db):
        from agents.runtime import workflow_engine as wf
        rs.create_workflow(workflow_type="promotion", symbol="NIFTY", first_stage=wf.STAGE_MARKET_DATA, state={})
        overview = runtime_api.get_runtime_overview(db_path=agent_db)
        assert len(overview["workflows_running"]) == 1

    def test_reflects_the_active_policy(self, agent_db):
        from agents.runtime import policy_engine as pe
        pe.set_policy("paper_trading", changed_by="t", reason="x")
        overview = runtime_api.get_runtime_overview(db_path=agent_db)
        assert overview["policy"] == "paper_trading"


class TestGetWorkflowDetail:
    def test_returns_none_for_unknown_id(self, agent_db):
        assert runtime_api.get_workflow_detail(99999) is None

    def test_includes_full_history(self, agent_db):
        from agents.runtime import workflow_engine as wf
        wid = rs.create_workflow(workflow_type="promotion", symbol="NIFTY", first_stage=wf.STAGE_MARKET_DATA, state={})
        rs.record_stage_transition(wid, stage=wf.STAGE_MARKET_DATA, status="completed", detail={})
        detail = runtime_api.get_workflow_detail(wid)
        assert detail["symbol"] == "NIFTY"
        assert len(detail["history"]) == 2  # started + completed


class TestGetQueueDetail:
    def test_reflects_queue_state(self, agent_db):
        from agents.runtime import task_queue
        task_queue.enqueue(priority="high", task_type="t", payload=None)
        detail = runtime_api.get_queue_detail()
        assert detail["depth"]["queued"] == 1
