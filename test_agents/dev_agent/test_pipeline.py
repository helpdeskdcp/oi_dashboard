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
from agents.dev_agent import detector, patcher, pipeline, worktree, approval_engine
from agents.dev_agent.gates.base import GateResult, GateStatus
from .conftest import commit_on_new_branch, git


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


def _detection(refused=False, **overrides):
    fields = dict(
        trigger="synthetic trigger", target_files=["feature.py"],
        issue_summary="synthetic issue", root_cause="synthetic root cause",
        confidence_score=70, suggested_files=["feature.py"], provider_used="openai",
        refused=refused, refusal_reason="refused for test" if refused else None,
    )
    fields.update(overrides)
    return detector.DetectionResult(**fields)


def _fake_generate_patch(repo_dir, detection, *, base_ref="main", provider_name=None, filename="feature.py",
                          content="VALUE = 42\n"):
    """Stands in for patcher.generate_patch(): creates a REAL worktree
    (via pipeline.worktree.create -- the same module pipeline.py itself
    calls) with one real committed file change, so everything downstream
    (patch_generator.changed_files/generate, the gates, worktree cleanup)
    exercises real git rather than a second layer of mocking."""
    wt = pipeline.worktree.create(detection.trigger, repo_dir=repo_dir, base_ref=base_ref)
    with open(wt.path + f"/{filename}", "w") as fh:
        fh.write(content)
    git(wt.path, "add", filename)
    git(wt.path, "commit", "-q", "-m", "agent: synthetic patch")
    proposal = patcher.PatchProposal(
        rationale="why: fixes a synthetic bug", expected_impact="metric X improves",
        risk_assessment="low risk", confidence_score=91,
        files_written=[filename], tests_written=[], docs_written=[], provider_used="claude",
    )
    return wt, proposal


class TestRunProposalDetectionRefused:
    def test_refused_detection_never_creates_a_worktree_and_is_hard_rejected(self, agent_db, toy_repo, monkeypatch):
        monkeypatch.setattr(pipeline.detector, "detect", lambda *a, **k: _detection(refused=True))

        def must_not_be_called(*a, **k):
            raise AssertionError("patcher.generate_patch called despite a refused detection")

        monkeypatch.setattr(pipeline.patcher, "generate_patch", must_not_be_called)

        result = pipeline.run_proposal(str(toy_repo), "trigger", ["agents/base_agent.py"], base_ref="main")

        assert result.decision == approval_engine.Decision.REJECTED
        assert result.gate_results == []
        assert result.worktree_removed is True
        row = audit_log.get(result.audit_log_id)
        assert row["risk_tier"] == "hard_blocked"
        assert row["outcome"] == "rejected"


class TestRunProposalPatchGenerationFailure:
    def test_self_modification_refusal_from_patcher_is_hard_rejected(self, agent_db, toy_repo, monkeypatch):
        monkeypatch.setattr(pipeline.detector, "detect", lambda *a, **k: _detection())

        def refuse(*a, **k):
            raise patcher.SelfModificationRefused("LLM proposed a guarded write")

        monkeypatch.setattr(pipeline.patcher, "generate_patch", refuse)

        result = pipeline.run_proposal(str(toy_repo), "trigger", ["feature.py"], base_ref="main")

        assert result.decision == approval_engine.Decision.REJECTED
        assert result.gate_results[0].gate == "patch_generation"
        assert result.gate_results[0].status == GateStatus.FAILED
        row = audit_log.get(result.audit_log_id)
        assert row["risk_tier"] == "hard_blocked"
        assert row["outcome"] == "rejected"

    def test_llm_or_parse_failure_is_rejected_needs_approval_tier(self, agent_db, toy_repo, monkeypatch):
        monkeypatch.setattr(pipeline.detector, "detect", lambda *a, **k: _detection())

        def boom(*a, **k):
            raise ValueError("LLM response contained no file/test/doc content to write")

        monkeypatch.setattr(pipeline.patcher, "generate_patch", boom)

        result = pipeline.run_proposal(str(toy_repo), "trigger", ["feature.py"], base_ref="main")

        assert result.decision == approval_engine.Decision.REJECTED
        row = audit_log.get(result.audit_log_id)
        assert row["risk_tier"] == "needs_approval"
        assert "no file/test/doc content" in row["payload_json"]["error"]


class TestRunProposalHappyPath:
    def test_all_gates_pass_yields_approved_with_full_patch_report(self, agent_db, toy_repo, monkeypatch):
        monkeypatch.setattr(pipeline.detector, "detect", lambda *a, **k: _detection())
        monkeypatch.setattr(pipeline.patcher, "generate_patch", _fake_generate_patch)
        _patch_all_gates_pass(monkeypatch)

        result = pipeline.run_proposal(str(toy_repo), "unit test failing on feature.py", ["feature.py"], base_ref="main")

        assert result.decision == approval_engine.Decision.APPROVED
        row = audit_log.get(result.audit_log_id)
        assert row["outcome"] == "pending_approval"
        assert row["payload_json"]["changed_files"] == ["feature.py"]

        report = row["payload_json"]["patch_report"]
        assert report["why"] == "why: fixes a synthetic bug"
        assert report["expected_impact"] == "metric X improves"
        assert report["risk_assessment"] == "low risk"
        assert report["confidence_score"] == 91
        assert report["provider_used"] == "claude"
        assert report["files_written"] == ["feature.py"]
        assert len(report["test_results"]) == 2  # unit_tests + integration_tests
        assert report["benchmark_comparison"]["gate"] == "benchmark"

        assert row["payload_json"]["detection"]["issue_summary"] == "synthetic issue"

    def test_worktree_kept_for_review_by_default(self, agent_db, toy_repo, monkeypatch):
        monkeypatch.setattr(pipeline.detector, "detect", lambda *a, **k: _detection())
        monkeypatch.setattr(pipeline.patcher, "generate_patch", _fake_generate_patch)
        _patch_all_gates_pass(monkeypatch)

        result = pipeline.run_proposal(str(toy_repo), "trigger", ["feature.py"], base_ref="main")

        assert result.worktree_removed is False
        remaining = worktree.list_worktrees(repo_dir=str(toy_repo))
        assert len(remaining) >= 1


class TestRunProposalGateFailure:
    def test_a_failing_gate_rejects_and_rolls_back_the_worktree(self, agent_db, toy_repo, monkeypatch):
        monkeypatch.setattr(pipeline.detector, "detect", lambda *a, **k: _detection())
        monkeypatch.setattr(pipeline.patcher, "generate_patch", _fake_generate_patch)
        monkeypatch.setattr(pipeline.unit_tests, "run", lambda path: _failing("unit_tests", "3 tests failed"))

        before = len(worktree.list_worktrees(repo_dir=str(toy_repo)))
        result = pipeline.run_proposal(str(toy_repo), "trigger", ["feature.py"], base_ref="main")
        after = len(worktree.list_worktrees(repo_dir=str(toy_repo)))

        assert result.decision == approval_engine.Decision.REJECTED
        assert result.worktree_removed is True
        assert after == before
        row = audit_log.get(result.audit_log_id)
        assert "3 tests failed" in row["payload_json"]["reasoning"]

    def test_self_modification_in_actual_diff_is_hard_rejected(self, agent_db, toy_repo, monkeypatch):
        # Defense in depth: even if detector/patcher somehow let a guarded
        # path through, pipeline.py's own re-check on the real diff still
        # catches it before any gate runs.
        monkeypatch.setattr(pipeline.detector, "detect", lambda *a, **k: _detection())

        def fake_patch_touching_guard(repo_dir, detection, *, base_ref="main", provider_name=None):
            return _fake_generate_patch(
                repo_dir, detection, base_ref=base_ref, filename="agents/sneaky.py", content="# tampered\n"
            )

        monkeypatch.setattr(pipeline.patcher, "generate_patch", fake_patch_touching_guard)

        def must_not_be_called(*a, **k):
            raise AssertionError("a gate ran despite the self-modification guard")

        monkeypatch.setattr(pipeline.unit_tests, "run", must_not_be_called)

        result = pipeline.run_proposal(str(toy_repo), "trigger", ["feature.py"], base_ref="main")

        assert result.decision == approval_engine.Decision.REJECTED
        row = audit_log.get(result.audit_log_id)
        assert row["risk_tier"] == "hard_blocked"
