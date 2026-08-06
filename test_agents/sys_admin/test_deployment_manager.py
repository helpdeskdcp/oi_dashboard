"""
test_agents/sys_admin/test_deployment_manager.py -- regression tests for
deployment_manager.py against a real throwaway git repo (never this
project's own repo). run_gates is monkeypatched for the merge-readiness
tests (avoids a real ~minute-long nested pytest run per test -- same
reasoning agents/quant_researcher and agents/trading_supervisor's own
tests already apply to the same function).
"""
from agents.dev_agent.gates.base import GateResult, GateStatus
from agents.sys_admin import deployment_manager

from .conftest import commit_on_new_branch, git


class TestCurrentVersion:
    def test_returns_commit_and_branch(self, toy_repo):
        info = deployment_manager.current_version(repo_dir=str(toy_repo))
        assert info["commit"]
        assert info["branch"] == "main"
        assert "error" not in info

    def test_not_a_git_repo_degrades_gracefully(self, tmp_path):
        info = deployment_manager.current_version(repo_dir=str(tmp_path))
        assert info["commit"] is None
        assert "error" in info


class TestIsFastForwardMergeable:
    def test_true_for_a_branch_with_no_divergence(self, toy_repo):
        commit_on_new_branch(toy_repo, "feature", "f.py", "x = 1\n")
        assert deployment_manager.is_fast_forward_mergeable("feature", repo_dir=str(toy_repo)) is True

    def test_false_when_base_has_diverged(self, toy_repo):
        commit_on_new_branch(toy_repo, "feature", "f.py", "x = 1\n")
        # advance main independently -- feature no longer contains main's tip
        git(toy_repo, "commit", "--allow-empty", "-q", "-m", "unrelated main commit")
        assert deployment_manager.is_fast_forward_mergeable("feature", repo_dir=str(toy_repo)) is False


class TestVerifyMergeReadiness:
    def _stub_gates(self, monkeypatch, results):
        monkeypatch.setattr(deployment_manager, "run_gates", lambda wt_path, repo_dir, files: results)

    def test_ff_mergeable_and_all_gates_passed(self, toy_repo, monkeypatch):
        commit_on_new_branch(toy_repo, "feature", "f.py", "x = 1\n")
        self._stub_gates(monkeypatch, [GateResult(gate="unit_tests", status=GateStatus.PASSED, summary="ok")])

        report = deployment_manager.verify_merge_readiness("feature", repo_dir=str(toy_repo))

        assert report.ff_mergeable is True
        assert report.all_gates_passed is True
        assert "fast-forward mergeable and all gates passed" in report.reason

    def test_failed_gate_is_reported(self, toy_repo, monkeypatch):
        commit_on_new_branch(toy_repo, "feature", "f.py", "x = 1\n")
        self._stub_gates(monkeypatch, [GateResult(gate="unit_tests", status=GateStatus.FAILED, summary="broken")])

        report = deployment_manager.verify_merge_readiness("feature", repo_dir=str(toy_repo))

        assert report.all_gates_passed is False
        assert "gate(s) failed" in report.reason

    def test_never_actually_merges(self, toy_repo, monkeypatch):
        commit_on_new_branch(toy_repo, "feature", "f.py", "x = 1\n")
        self._stub_gates(monkeypatch, [GateResult(gate="unit_tests", status=GateStatus.PASSED, summary="ok")])

        before_head = git(toy_repo, "rev-parse", "HEAD")
        deployment_manager.verify_merge_readiness("feature", repo_dir=str(toy_repo))
        after_head = git(toy_repo, "rev-parse", "HEAD")

        assert before_head == after_head  # main's HEAD is completely untouched
        assert "f.py" not in git(toy_repo, "ls-files")  # the file never landed on main


class TestRollbackDelegatesToRollbackModule:
    def test_calls_the_shared_rollback_module(self, monkeypatch):
        calls = {}
        monkeypatch.setattr(
            deployment_manager.rollback_module, "rollback",
            lambda audit_log_id, **k: calls.update(audit_log_id=audit_log_id, kwargs=k) or "reverted",
        )
        result = deployment_manager.rollback(42, repo_dir="/some/repo")
        assert result == "reverted"
        assert calls["audit_log_id"] == 42
        assert calls["kwargs"] == {"repo_dir": "/some/repo"}
