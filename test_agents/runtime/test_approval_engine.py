import pytest

from agents import audit_log
from agents.runtime import approval_engine as ae
from test_agents.dev_agent.conftest import commit_on_new_branch, git


def _make_proposal(*, branch="feature-x", base_ref="main"):
    return audit_log.record(
        agent="dev_agent", action_type="proposal", description="test proposal",
        risk_tier="needs_approval", outcome="pending_approval",
        payload={"branch": branch, "base_ref": base_ref, "changed_files": ["feature.py"]},
    )


class TestProposalLifecycle:
    def test_list_pending_shows_a_new_proposal(self, agent_db):
        row_id = _make_proposal()
        assert row_id in [r["id"] for r in ae.list_pending_proposals()]

    def test_approve_then_reject_raises_wrong_state(self, agent_db):
        row_id = _make_proposal()
        ae.approve_proposal(row_id, approved_by="human1")
        with pytest.raises(ValueError):
            ae.reject_proposal(row_id, rejected_by="human1")

    def test_approve_unknown_row_raises(self, agent_db):
        with pytest.raises(ValueError):
            ae.approve_proposal(99999, approved_by="human1")

    def test_reject_marks_rejected(self, agent_db):
        row_id = _make_proposal()
        row = ae.reject_proposal(row_id, rejected_by="human1")
        assert row["outcome"] == "rejected"

    def test_apply_before_approve_raises(self, agent_db, toy_repo):
        row_id = _make_proposal()
        with pytest.raises(ValueError):
            ae.apply_proposal(row_id, applied_by="human1", repo_dir=str(toy_repo))

    def test_apply_with_no_branch_in_payload_raises(self, agent_db, toy_repo):
        row_id = audit_log.record(
            agent="dev_agent", action_type="proposal", description="no branch",
            risk_tier="needs_approval", outcome="pending_approval", payload={},
        )
        ae.approve_proposal(row_id, approved_by="human1")
        with pytest.raises(ValueError):
            ae.apply_proposal(row_id, applied_by="human1", repo_dir=str(toy_repo))


class TestApplyRealMerge:
    def test_apply_actually_merges_the_branch(self, agent_db, toy_repo):
        commit_on_new_branch(toy_repo, "feature-x", "feature.py", "x = 1\n")
        row_id = _make_proposal(branch="feature-x")
        ae.approve_proposal(row_id, approved_by="human1")

        result = ae.apply_proposal(row_id, applied_by="human1", repo_dir=str(toy_repo))

        assert result.merged is True
        assert (toy_repo / "feature.py").exists()
        row = audit_log.get(row_id)
        assert row["outcome"] == "applied"
        assert row["merge_commit_sha"] == result.merge_commit_sha

    def test_apply_refuses_when_no_longer_fast_forward_mergeable(self, agent_db, toy_repo):
        commit_on_new_branch(toy_repo, "feature-x", "feature.py", "x = 1\n")
        row_id = _make_proposal(branch="feature-x")
        ae.approve_proposal(row_id, approved_by="human1")

        # main moves forward with a DIFFERENT change after the proposal
        # was created -- feature-x can no longer fast-forward.
        (toy_repo / "other.py").write_text("y = 2\n")
        git(toy_repo, "add", "other.py")
        git(toy_repo, "commit", "-q", "-m", "unrelated change on main")

        with pytest.raises(ValueError):
            ae.apply_proposal(row_id, applied_by="human1", repo_dir=str(toy_repo))
        row = audit_log.get(row_id)
        assert row["outcome"] == "approved"  # unchanged -- never partially applied

    def test_rollback_can_find_the_merge_commit_sha_afterward(self, agent_db, toy_repo):
        """agents.rollback.rollback() reads row['merge_commit_sha'] --
        confirms apply_proposal() actually writes what that module needs."""
        commit_on_new_branch(toy_repo, "feature-x", "feature.py", "x = 1\n")
        row_id = _make_proposal(branch="feature-x")
        ae.approve_proposal(row_id, approved_by="human1")
        ae.apply_proposal(row_id, applied_by="human1", repo_dir=str(toy_repo))

        from agents import rollback
        result = rollback.rollback(row_id, repo_dir=str(toy_repo))
        assert result
        assert not (toy_repo / "feature.py").exists()


class TestWorkflowApprovals:
    def test_list_and_approve_a_waiting_workflow(self, agent_db, memory_store):
        from agents.runtime import policy_engine as pe
        from agents.runtime import runtime_store as rs
        from agents.runtime import workflow_engine as wf

        pe.set_policy("semi_auto", changed_by="t", reason="x")
        wid = rs.create_workflow(
            workflow_type="promotion", symbol="NIFTY", first_stage=wf.STAGE_HUMAN_APPROVAL,
            state={"symbol": "NIFTY", "decision": "APPROVED"},
        )
        wf.advance(wid, memory_store=memory_store)
        assert wid in [w["id"] for w in ae.list_pending_workflows()]

        w = ae.approve_workflow(wid, approved_by="human1", reason="ok")
        assert w["status"] == "running"

    def test_reject_a_waiting_workflow(self, agent_db, memory_store):
        from agents.runtime import policy_engine as pe
        from agents.runtime import runtime_store as rs
        from agents.runtime import workflow_engine as wf

        pe.set_policy("semi_auto", changed_by="t", reason="x")
        wid = rs.create_workflow(
            workflow_type="promotion", symbol="NIFTY", first_stage=wf.STAGE_HUMAN_APPROVAL,
            state={"symbol": "NIFTY", "decision": "APPROVED"},
        )
        wf.advance(wid, memory_store=memory_store)
        w = ae.reject_workflow(wid, rejected_by="human1", reason="not confident")
        assert w["status"] == "cancelled"
