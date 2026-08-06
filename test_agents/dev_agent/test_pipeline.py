"""
test_agents/dev_agent/test_pipeline.py -- end-to-end tests for
agents/dev_agent/pipeline.py against a real throwaway git repo, with
every gate's `run` monkeypatched to a synthetic result -- exactly the
plan's own prescription: "end-to-end against a seeded, deliberately-
broken TOY fixture -- never against the real app; covers dry-run vs.
live and full rejection." No LLM call, no real backtest, no real pytest
subprocess.
"""
import pytest

from agents import audit_log
from agents.dev_agent import pipeline, worktree, approval_engine
from agents.dev_agent.gates.base import GateResult, GateStatus
from .conftest import commit_on_new_branch


def _passing(gate_name):
    return GateResult(gate=gate_name, status=GateStatus.PASSED, summary="synthetic pass")


def _failing(gate_name, summary="synthetic failure"):
    return GateResult(gate=gate_name, status=GateStatus.FAILED, summary=summary)


def _patch_all_gates_pass(monkeypatch):
    monkeypatch.setattr(pipeline.unit_tests, "run", lambda path: _passing("unit_tests"))
    monkeypatch.setattr(pipeline.integration_tests, "run", lambda path: _passing("integration_tests"))
    monkeypatch.setattr(
        pipeline.backtest_compare, "run",
        lambda base, cand, files: GateResult(
            gate="backtest_compare", status=GateStatus.SKIPPED,
            summary="no strategy file touched", details={},
        ),
    )
    monkeypatch.setattr(
        pipeline.benchmark, "run",
        lambda bt_result: GateResult(gate="benchmark", status=GateStatus.SKIPPED, summary="nothing to benchmark"),
    )
    monkeypatch.setattr(pipeline.code_quality, "run", lambda path: _passing("code_quality"))


class TestPipelineHappyPath:
    def test_all_gates_pass_yields_approved_and_pending_approval(self, agent_db, toy_repo, monkeypatch):
        _patch_all_gates_pass(monkeypatch)
        commit_on_new_branch(toy_repo, "agent/dev-candidate-ok", "feature.py", "VALUE = 1\n")

        result = pipeline.run("agent/dev-candidate-ok", repo_dir=str(toy_repo), base_ref="main")

        assert result.decision == approval_engine.Decision.APPROVED
        row = audit_log.get(result.audit_log_id)
        assert row["outcome"] == "pending_approval"
        assert row["payload_json"]["changed_files"] == ["feature.py"]
        assert "VALUE = 1" in row["payload_json"]["diff"]

    def test_worktree_is_kept_for_review_on_approval_by_default(self, agent_db, toy_repo, monkeypatch):
        _patch_all_gates_pass(monkeypatch)
        commit_on_new_branch(toy_repo, "agent/dev-candidate-keep", "feature2.py", "VALUE = 2\n")

        result = pipeline.run("agent/dev-candidate-keep", repo_dir=str(toy_repo), base_ref="main")

        assert result.worktree_removed is False
        remaining = worktree.list_worktrees(repo_dir=str(toy_repo))
        assert any("candidate-keep" in e["path"] for e in remaining)

    def test_keep_worktree_on_success_false_removes_it_anyway(self, agent_db, toy_repo, monkeypatch):
        _patch_all_gates_pass(monkeypatch)
        commit_on_new_branch(toy_repo, "agent/dev-candidate-noreview", "feature3.py", "VALUE = 3\n")

        result = pipeline.run(
            "agent/dev-candidate-noreview", repo_dir=str(toy_repo), base_ref="main",
            keep_worktree_on_success=False,
        )

        assert result.worktree_removed is True


class TestPipelineFailureRecovery:
    def test_a_failed_gate_stops_immediately_and_does_not_run_later_gates(self, agent_db, toy_repo, monkeypatch):
        call_order = []

        monkeypatch.setattr(pipeline.unit_tests, "run", lambda path: (call_order.append("unit_tests"), _failing("unit_tests"))[1])

        def integration_should_not_run(path):
            call_order.append("integration_tests")
            return _passing("integration_tests")

        monkeypatch.setattr(pipeline.integration_tests, "run", integration_should_not_run)
        commit_on_new_branch(toy_repo, "agent/dev-candidate-broken", "broken.py", "1/0\n")

        result = pipeline.run("agent/dev-candidate-broken", repo_dir=str(toy_repo), base_ref="main")

        assert result.decision == approval_engine.Decision.REJECTED
        assert call_order == ["unit_tests"]  # integration_tests never called

    def test_failed_gate_rolls_back_the_worktree(self, agent_db, toy_repo, monkeypatch):
        monkeypatch.setattr(pipeline.unit_tests, "run", lambda path: _failing("unit_tests"))
        commit_on_new_branch(toy_repo, "agent/dev-candidate-rollback", "broken2.py", "raise RuntimeError\n")

        before = len(worktree.list_worktrees(repo_dir=str(toy_repo)))
        result = pipeline.run("agent/dev-candidate-rollback", repo_dir=str(toy_repo), base_ref="main")
        after = len(worktree.list_worktrees(repo_dir=str(toy_repo)))

        assert result.worktree_removed is True
        assert after == before

    def test_failed_run_still_writes_a_complete_audit_row(self, agent_db, toy_repo, monkeypatch):
        monkeypatch.setattr(pipeline.unit_tests, "run", lambda path: _failing("unit_tests", "3 tests failed"))
        commit_on_new_branch(toy_repo, "agent/dev-candidate-audited", "broken3.py", "x = 1\n")

        result = pipeline.run("agent/dev-candidate-audited", repo_dir=str(toy_repo), base_ref="main")

        row = audit_log.get(result.audit_log_id)
        assert row["outcome"] == "rejected"
        assert "3 tests failed" in row["payload_json"]["reasoning"]
        assert row["payload_json"]["gates"][0]["status"] == "failed"

    def test_unexpected_exception_still_rolls_back_and_propagates(self, agent_db, toy_repo, monkeypatch):
        def boom(path):
            raise RuntimeError("gate blew up unexpectedly")

        monkeypatch.setattr(pipeline.unit_tests, "run", boom)
        commit_on_new_branch(toy_repo, "agent/dev-candidate-crash", "crash.py", "x = 1\n")

        before = len(worktree.list_worktrees(repo_dir=str(toy_repo)))
        with pytest.raises(RuntimeError, match="gate blew up"):
            pipeline.run("agent/dev-candidate-crash", repo_dir=str(toy_repo), base_ref="main")
        after = len(worktree.list_worktrees(repo_dir=str(toy_repo)))

        assert after == before  # worktree still cleaned up despite the crash


class TestPipelineSelfModificationGuard:
    def test_diff_touching_agents_directory_is_hard_rejected_before_any_gate_runs(self, agent_db, toy_repo, monkeypatch):
        def must_not_be_called(*args, **kwargs):
            raise AssertionError("a gate ran despite the self-modification guard")

        monkeypatch.setattr(pipeline.unit_tests, "run", must_not_be_called)
        monkeypatch.setattr(pipeline.integration_tests, "run", must_not_be_called)
        monkeypatch.setattr(pipeline.backtest_compare, "run", must_not_be_called)
        monkeypatch.setattr(pipeline.benchmark, "run", must_not_be_called)
        monkeypatch.setattr(pipeline.code_quality, "run", must_not_be_called)

        commit_on_new_branch(toy_repo, "agent/dev-self-mod", "agents/base_agent.py", "# tampered\n")

        result = pipeline.run("agent/dev-self-mod", repo_dir=str(toy_repo), base_ref="main")

        assert result.decision == approval_engine.Decision.REJECTED
        row = audit_log.get(result.audit_log_id)
        assert row["risk_tier"] == "hard_blocked"
        assert row["outcome"] == "rejected"
        assert result.worktree_removed is True
