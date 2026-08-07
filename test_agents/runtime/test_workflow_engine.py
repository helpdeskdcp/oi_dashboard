import pytest

from agents.runtime import policy_engine as pe
from agents.runtime import runtime_store as rs
from agents.runtime import workflow_engine as wf


def _make_wf_at_approval(*, decision="APPROVED"):
    return rs.create_workflow(
        workflow_type="promotion", symbol="NIFTY", first_stage=wf.STAGE_HUMAN_APPROVAL,
        state={"symbol": "NIFTY", "decision": decision},
    )


class TestStart:
    def test_start_creates_a_workflow_at_market_data(self, agent_db):
        wid = wf.start("NIFTY")
        w = rs.get_workflow(wid)
        assert w["current_stage"] == wf.STAGE_MARKET_DATA
        assert w["status"] == "running"

    def test_start_refuses_when_policy_is_read_only(self, agent_db):
        pe.set_policy("read_only", changed_by="t", reason="x")
        with pytest.raises(wf.WorkflowError):
            wf.start("NIFTY")

    def test_start_refuses_during_emergency_stop(self, agent_db):
        pe.set_policy("emergency_stop", changed_by="t", reason="x")
        with pytest.raises(wf.WorkflowError):
            wf.start("NIFTY")


class TestMarketDataStage:
    def test_advances_to_research_when_candles_exist(self, agent_db, memory_store):
        wid = wf.start("NIFTY")
        status = wf.advance(wid, memory_store=memory_store)
        assert status == "running"
        assert rs.get_workflow(wid)["current_stage"] == wf.STAGE_RESEARCH

    def test_fails_when_no_candle_archive_exists(self, agent_db, memory_store):
        wid = wf.start("NOT_A_REAL_SYMBOL_XYZ")
        status = wf.advance(wid, memory_store=memory_store)
        assert status == "failed"


class TestResearchStage:
    def test_completes_when_zero_hypotheses_validate(self, agent_db, memory_store):
        """In this test environment there's no live oi_history.db
        cycles table, so every hypothesis fails validation -- a real,
        honest 'nothing to submit' outcome, not a failure."""
        wid = wf.start("NIFTY")
        wf.advance(wid, memory_store=memory_store)  # market_data -> research
        status = wf.advance(wid, memory_store=memory_store, repo_dir=".")  # research
        assert status == "completed"
        assert rs.get_workflow(wid)["current_stage"] == wf.STAGE_RESEARCH


class TestHumanApprovalStage:
    def test_recommendation_only_stops_here(self, agent_db, memory_store):
        pe.set_policy("recommendation_only", changed_by="t", reason="x")
        wid = _make_wf_at_approval()
        status = wf.advance(wid, memory_store=memory_store)
        assert status == "completed"
        assert rs.get_workflow(wid)["current_stage"] == wf.STAGE_HUMAN_APPROVAL

    def test_semi_auto_waits_for_a_human(self, agent_db, memory_store):
        pe.set_policy("semi_auto", changed_by="t", reason="x")
        wid = _make_wf_at_approval()
        status = wf.advance(wid, memory_store=memory_store)
        assert status == "waiting_approval"

    def test_full_auto_advances_without_waiting(self, agent_db, memory_store):
        pe.set_policy("full_auto", changed_by="t", reason="x")
        wid = _make_wf_at_approval()
        status = wf.advance(wid, memory_store=memory_store)
        assert status == "running"
        assert rs.get_workflow(wid)["current_stage"] == wf.STAGE_EXECUTION

    def test_full_auto_never_touches_the_code_promotion_audit_log(self, agent_db, memory_store):
        """The critical safety assertion: full_auto's auto-approve is a
        WORKFLOW-level convenience, never a code/strategy merge
        approval -- agent_audit_log must be completely untouched."""
        from agents import audit_log
        pe.set_policy("full_auto", changed_by="t", reason="x")
        wid = _make_wf_at_approval()
        wf.advance(wid, memory_store=memory_store)
        assert audit_log.list_pending() == []


class TestApproveRejectWorkflow:
    def test_approve_moves_to_execution_and_running(self, agent_db, memory_store):
        pe.set_policy("semi_auto", changed_by="t", reason="x")
        wid = _make_wf_at_approval()
        wf.advance(wid, memory_store=memory_store)  # -> waiting_approval
        wf.approve_workflow(wid, approved_by="human1", reason="looks fine")
        w = rs.get_workflow(wid)
        assert w["status"] == "running"
        assert w["current_stage"] == wf.STAGE_EXECUTION

    def test_approve_raises_if_not_waiting(self, agent_db):
        wid = wf.start("NIFTY")
        with pytest.raises(wf.WorkflowError):
            wf.approve_workflow(wid, approved_by="human1")

    def test_reject_cancels_the_workflow(self, agent_db, memory_store):
        pe.set_policy("semi_auto", changed_by="t", reason="x")
        wid = _make_wf_at_approval()
        wf.advance(wid, memory_store=memory_store)
        wf.reject_workflow(wid, rejected_by="human1", reason="not confident")
        assert rs.get_workflow(wid)["status"] == "cancelled"


class TestExecutionStage:
    def test_execution_never_places_an_order_only_a_recommendation(self, agent_db, memory_store):
        pe.set_policy("full_auto", changed_by="t", reason="x")
        wid = _make_wf_at_approval()
        wf.run_to_completion(wid, memory_store=memory_store)
        w = rs.get_workflow(wid)
        rec = w["state_json"]["execution_recommendation"]
        assert "no order" in rec["note"].lower()
        assert rec["recommended_quantity"] > 0


class TestFullRunToCompletion:
    def test_full_auto_drives_all_the_way_to_memory_update(self, agent_db, memory_store):
        pe.set_policy("full_auto", changed_by="t", reason="x")
        wid = _make_wf_at_approval()
        status = wf.run_to_completion(wid, memory_store=memory_store)
        assert status == "completed"
        w = rs.get_workflow(wid)
        assert w["current_stage"] == wf.STAGE_MEMORY_UPDATE
        stages = [h["stage"] for h in rs.workflow_history(wid)]
        assert wf.STAGE_LEARNING in stages


class TestResume:
    def test_resume_continues_from_the_persisted_stage_not_the_start(self, agent_db, memory_store):
        pe.set_policy("full_auto", changed_by="t", reason="x")
        wid = _make_wf_at_approval()
        wf.advance(wid, memory_store=memory_store)  # -> execution
        assert rs.get_workflow(wid)["current_stage"] == wf.STAGE_EXECUTION
        status = wf.resume(wid, memory_store=memory_store)
        assert status == "running"
        assert rs.get_workflow(wid)["current_stage"] == wf.STAGE_LEARNING


class TestEmergencyStopPausesInFlightWorkflows:
    def test_advance_pauses_without_failing_during_emergency_stop(self, agent_db, memory_store):
        wid = wf.start("NIFTY")
        pe.set_policy("emergency_stop", changed_by="t", reason="halt")
        status = wf.advance(wid, memory_store=memory_store)
        assert status == "running"
        assert rs.get_workflow(wid)["current_stage"] == wf.STAGE_MARKET_DATA  # unchanged
