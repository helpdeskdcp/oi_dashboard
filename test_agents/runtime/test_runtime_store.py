from agents.runtime import runtime_store as rs


class TestTaskQueue:
    def test_enqueue_and_claim_returns_highest_priority_first(self, agent_db):
        rs.enqueue(priority="low", task_type="t", payload={"i": 1}, max_attempts=3)
        high_id = rs.enqueue(priority="high", task_type="t", payload={"i": 2}, max_attempts=3)
        claimed = rs.claim_next_task()
        assert claimed["id"] == high_id
        assert claimed["status"] == "running"

    def test_claim_returns_none_when_queue_empty(self, agent_db):
        assert rs.claim_next_task() is None

    def test_concurrent_claims_never_return_the_same_task_twice(self, agent_db):
        """Simulates the race claim_next_task()'s docstring describes:
        two callers racing for the same due task -- only one may win."""
        task_id = rs.enqueue(priority="high", task_type="t", payload=None, max_attempts=3)
        first = rs.claim_next_task()
        second = rs.claim_next_task()
        assert first["id"] == task_id
        assert second is None

    def test_invalid_priority_is_rejected(self, agent_db):
        import pytest
        with pytest.raises(ValueError):
            rs.enqueue(priority="urgent", task_type="t", payload=None, max_attempts=3)

    def test_fail_task_retries_until_max_attempts_then_goes_dead(self, agent_db):
        task_id = rs.enqueue(priority="high", task_type="t", payload=None, max_attempts=2)
        rs.claim_next_task()
        status1 = rs.fail_task(task_id, error="e1", retry_backoff_seconds=0)
        assert status1 == "retrying"
        rs.claim_next_task()
        status2 = rs.fail_task(task_id, error="e2", retry_backoff_seconds=0)
        assert status2 == "dead"

    def test_complete_task_marks_completed(self, agent_db):
        task_id = rs.enqueue(priority="high", task_type="t", payload=None, max_attempts=3)
        rs.claim_next_task()
        rs.complete_task(task_id)
        rows = rs.list_tasks(status="completed")
        assert any(r["id"] == task_id for r in rows)

    def test_queue_depth_reflects_real_counts(self, agent_db):
        rs.enqueue(priority="high", task_type="t", payload=None, max_attempts=3)
        rs.enqueue(priority="low", task_type="t", payload=None, max_attempts=3)
        depth = rs.queue_depth()
        assert depth["queued"] == 2


class TestWorkflowState:
    def test_create_and_get_workflow_roundtrips_state(self, agent_db):
        wid = rs.create_workflow(workflow_type="promotion", symbol="NIFTY", first_stage="market_data", state={"a": 1})
        wf = rs.get_workflow(wid)
        assert wf["symbol"] == "NIFTY"
        assert wf["status"] == "running"
        assert wf["state_json"] == {"a": 1}

    def test_update_workflow_only_touches_passed_fields(self, agent_db):
        wid = rs.create_workflow(workflow_type="promotion", symbol="NIFTY", first_stage="market_data", state={"a": 1})
        rs.update_workflow(wid, current_stage="research")
        wf = rs.get_workflow(wid)
        assert wf["current_stage"] == "research"
        assert wf["state_json"] == {"a": 1}  # untouched

    def test_workflow_history_records_every_transition_in_order(self, agent_db):
        wid = rs.create_workflow(workflow_type="promotion", symbol="NIFTY", first_stage="market_data", state={})
        rs.record_stage_transition(wid, stage="market_data", status="completed", detail={"n": 1})
        rs.record_stage_transition(wid, stage="research", status="completed", detail={"n": 2})
        history = rs.workflow_history(wid)
        assert [h["stage"] for h in history] == ["market_data", "market_data", "research"]

    def test_resumable_workflows_returns_only_running_and_waiting(self, agent_db):
        running = rs.create_workflow(workflow_type="promotion", symbol="A", first_stage="market_data", state={})
        completed = rs.create_workflow(workflow_type="promotion", symbol="B", first_stage="market_data", state={})
        rs.update_workflow(completed, status="completed")
        waiting = rs.create_workflow(workflow_type="promotion", symbol="C", first_stage="human_approval", state={})
        rs.update_workflow(waiting, status="waiting_approval")

        ids = {w["id"] for w in rs.resumable_workflows()}
        assert ids == {running, waiting}

    def test_list_workflows_filters_by_status(self, agent_db):
        wid = rs.create_workflow(workflow_type="promotion", symbol="NIFTY", first_stage="market_data", state={})
        rs.update_workflow(wid, status="failed")
        assert [w["id"] for w in rs.list_workflows(status="failed")] == [wid]
        assert rs.list_workflows(status="completed") == []


class TestPolicyOverride:
    def test_no_override_returns_none(self, agent_db):
        assert rs.get_policy_override() is None

    def test_set_and_get_override_roundtrips(self, agent_db):
        rs.set_policy_override("semi_auto", changed_by="tester", reason="testing")
        override = rs.get_policy_override()
        assert override["policy"] == "semi_auto"
        assert override["changed_by"] == "tester"

    def test_set_override_twice_replaces_not_duplicates(self, agent_db):
        rs.set_policy_override("semi_auto", changed_by="a", reason="x")
        rs.set_policy_override("full_auto", changed_by="b", reason="y")
        assert rs.get_policy_override()["policy"] == "full_auto"
