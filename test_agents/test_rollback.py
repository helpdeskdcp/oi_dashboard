"""
test_agents/test_rollback.py -- regression tests for agents/rollback.py.
Exercises real `git revert` against a throwaway git repo in tmp_path --
never against this project's own repo.
"""
import subprocess

import pytest

from agents import audit_log, rollback


def _git(repo_dir, *args):
    result = subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture()
def toy_repo(tmp_path):
    repo_dir = tmp_path / "toy_repo"
    repo_dir.mkdir()
    _git(repo_dir, "init", "-q")
    _git(repo_dir, "config", "user.email", "test@example.com")
    _git(repo_dir, "config", "user.name", "Test")
    (repo_dir / "file.txt").write_text("original\n")
    _git(repo_dir, "add", "file.txt")
    _git(repo_dir, "commit", "-q", "-m", "initial")

    (repo_dir / "file.txt").write_text("changed by the applied proposal\n")
    _git(repo_dir, "add", "file.txt")
    _git(repo_dir, "commit", "-q", "-m", "applied proposal")
    merge_sha = _git(repo_dir, "rev-parse", "HEAD")

    return repo_dir, merge_sha


class TestRollback:
    def test_reverts_the_recorded_merge_and_updates_outcome(self, agent_db, toy_repo):
        repo_dir, merge_sha = toy_repo
        row_id = audit_log.record(
            agent="dev", action_type="applied", description="applied a proposal",
            risk_tier="needs_approval", outcome="applied",
        )
        audit_log.set_outcome(row_id, "applied", merge_commit_sha=merge_sha, pre_merge_sha="whatever")

        rollback.rollback(row_id, repo_dir=str(repo_dir))

        assert (repo_dir / "file.txt").read_text() == "original\n"
        assert audit_log.get(row_id)["outcome"] == "rolled_back"

    def test_logs_a_separate_rollback_row_not_overwriting_the_original(self, agent_db, toy_repo):
        repo_dir, merge_sha = toy_repo
        row_id = audit_log.record(
            agent="dev", action_type="applied", description="original proposal description",
            risk_tier="needs_approval", outcome="applied", payload={"diff": "original diff"},
        )
        audit_log.set_outcome(row_id, "applied", merge_commit_sha=merge_sha)

        rollback.rollback(row_id, repo_dir=str(repo_dir))

        original_row = audit_log.get(row_id)
        assert original_row["description"] == "original proposal description"   # untouched
        assert original_row["payload_json"] == {"diff": "original diff"}         # untouched

        # rollback() only ever INSERTs a new row (via audit_log.record) plus
        # one UPDATE to the original row's outcome column -- with row_id
        # being the only row before this call, the rollback row is exactly
        # the next id.
        rollback_row = audit_log.get(row_id + 1)
        assert rollback_row["action_type"] == "rollback"
        assert rollback_row["payload_json"]["target_audit_log_id"] == row_id

    def test_unknown_audit_log_id_raises(self, agent_db, toy_repo):
        repo_dir, _ = toy_repo
        with pytest.raises(rollback.RollbackError):
            rollback.rollback(99999, repo_dir=str(repo_dir))

    def test_row_not_applied_raises(self, agent_db, toy_repo):
        repo_dir, merge_sha = toy_repo
        row_id = audit_log.record(
            agent="dev", action_type="proposal", description="still pending",
            risk_tier="needs_approval", outcome="pending_approval",
        )
        with pytest.raises(rollback.RollbackError, match="expected 'applied'"):
            rollback.rollback(row_id, repo_dir=str(repo_dir))

    def test_missing_merge_sha_raises(self, agent_db, toy_repo):
        repo_dir, _ = toy_repo
        row_id = audit_log.record(
            agent="dev", action_type="applied", description="no sha recorded",
            risk_tier="needs_approval", outcome="applied",
        )
        with pytest.raises(rollback.RollbackError, match="no merge_commit_sha"):
            rollback.rollback(row_id, repo_dir=str(repo_dir))

    def test_failed_revert_logs_failure_and_raises(self, agent_db, toy_repo):
        repo_dir, _ = toy_repo
        row_id = audit_log.record(
            agent="dev", action_type="applied", description="bad sha",
            risk_tier="needs_approval", outcome="applied",
        )
        audit_log.set_outcome(row_id, "applied", merge_commit_sha="0000000000000000000000000000000000000000")

        with pytest.raises(rollback.RollbackError):
            rollback.rollback(row_id, repo_dir=str(repo_dir))

        # The original row must NOT have been marked rolled_back on a failed attempt.
        assert audit_log.get(row_id)["outcome"] == "applied"

    def test_never_calls_reset_hard(self):
        # Static guard -- this module's only subprocess.run call must
        # invoke `git revert`, never a destructive reset/clean. Scoped to
        # the function body (not the whole module) so the docstring's own
        # explanation of what NOT to do doesn't trip the check.
        import inspect

        body = inspect.getsource(rollback.rollback)
        assert body.count("subprocess.run") == 1
        assert '"revert"' in body
        assert "reset" not in body
        assert "--hard" not in body
