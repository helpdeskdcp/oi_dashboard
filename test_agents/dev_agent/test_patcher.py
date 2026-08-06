"""
test_agents/dev_agent/test_patcher.py -- regression tests for
agents/dev_agent/patcher.py against a real throwaway git repo (the same
toy_repo fixture Milestone 2's worktree/pipeline tests use). The LLM call
is monkeypatched at llm_providers.generate_with_fallback -- never a real
network call -- but git worktree/commit operations are real, so these
tests confirm the actual file-write/commit path works, not just that a
mock was invoked correctly.
"""
import json

import pytest

from agents.dev_agent import detector, patcher, worktree
from .conftest import git


def _detection(trigger="fix the bug", target_files=None, suggested_files=None):
    return detector.DetectionResult(
        trigger=trigger, target_files=target_files or ["feature.py"],
        issue_summary="off-by-one", root_cause="wrong loop bound", confidence_score=80,
        suggested_files=suggested_files if suggested_files is not None else (target_files or ["feature.py"]),
        provider_used="",
    )


def _llm_response(**overrides):
    payload = {
        "files": {"feature.py": "VALUE = 2\n"},
        "tests": {"test_feature.py": "def test_value():\n    assert True\n"},
        "docs": {},
        "rationale": "fixes the off-by-one",
        "expected_impact": "correct output",
        "risk_assessment": "low -- single constant change",
        "confidence_score": 85,
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestSelfModificationGuardOnDetectionInput:
    def test_refuses_before_creating_a_worktree(self, toy_repo, monkeypatch):
        def must_not_be_called(*a, **k):
            raise AssertionError("worktree.create() called despite a guarded suggested_files entry")

        monkeypatch.setattr(patcher.worktree, "create", must_not_be_called)

        detection = _detection(suggested_files=["agents/base_agent.py"])
        with pytest.raises(patcher.SelfModificationRefused):
            patcher.generate_patch(str(toy_repo), detection, base_ref="main")


class TestSelfModificationGuardOnLlmResponse:
    def test_refuses_and_rolls_back_when_llm_proposes_a_guarded_write(self, toy_repo, monkeypatch):
        monkeypatch.setattr(
            patcher.llm_providers, "generate_with_fallback",
            lambda *a, **k: (_llm_response(files={"agents/base_agent.py": "# tampered\n"}), "openai"),
        )

        before = len(worktree.list_worktrees(repo_dir=str(toy_repo)))
        detection = _detection()
        with pytest.raises(patcher.SelfModificationRefused):
            patcher.generate_patch(str(toy_repo), detection, base_ref="main")
        after = len(worktree.list_worktrees(repo_dir=str(toy_repo)))

        assert after == before  # the worktree created for this attempt was rolled back

    def test_refuses_when_llm_proposes_a_guarded_test_or_doc_path(self, toy_repo, monkeypatch):
        monkeypatch.setattr(
            patcher.llm_providers, "generate_with_fallback",
            lambda *a, **k: (_llm_response(tests={"agents/dev_agent/sneaky_test.py": "x = 1\n"}), "openai"),
        )
        detection = _detection()
        with pytest.raises(patcher.SelfModificationRefused):
            patcher.generate_patch(str(toy_repo), detection, base_ref="main")


class TestGeneratePatchHappyPath:
    def test_writes_files_tests_and_docs_and_commits(self, toy_repo, monkeypatch):
        monkeypatch.setattr(
            patcher.llm_providers, "generate_with_fallback",
            lambda *a, **k: (_llm_response(), "claude"),
        )
        detection = _detection()

        wt, proposal = patcher.generate_patch(str(toy_repo), detection, base_ref="main")
        try:
            assert proposal.files_written == ["feature.py"]
            assert proposal.tests_written == ["test_feature.py"]
            assert proposal.docs_written == []
            assert proposal.provider_used == "claude"
            assert proposal.confidence_score == 85
            assert proposal.rationale == "fixes the off-by-one"
            assert proposal.expected_impact == "correct output"
            assert proposal.risk_assessment == "low -- single constant change"

            written_content = (wt.path + "/feature.py")
            with open(written_content) as fh:
                assert fh.read() == "VALUE = 2\n"

            log = git(wt.path, "log", "-1", "--pretty=%s")
            assert "off-by-one" in log or "agent:" in log
        finally:
            worktree.remove(wt)

    def test_confidence_score_is_clamped(self, toy_repo, monkeypatch):
        monkeypatch.setattr(
            patcher.llm_providers, "generate_with_fallback",
            lambda *a, **k: (_llm_response(confidence_score=999), "openai"),
        )
        wt, proposal = patcher.generate_patch(str(toy_repo), _detection(), base_ref="main")
        try:
            assert proposal.confidence_score == 100
        finally:
            worktree.remove(wt)

    def test_raises_and_rolls_back_when_llm_response_has_no_content(self, toy_repo, monkeypatch):
        monkeypatch.setattr(
            patcher.llm_providers, "generate_with_fallback",
            lambda *a, **k: (_llm_response(files={}, tests={}, docs={}), "openai"),
        )
        before = len(worktree.list_worktrees(repo_dir=str(toy_repo)))
        with pytest.raises(ValueError, match="no file/test/doc content"):
            patcher.generate_patch(str(toy_repo), _detection(), base_ref="main")
        after = len(worktree.list_worktrees(repo_dir=str(toy_repo)))
        assert after == before

    def test_raises_and_rolls_back_on_malformed_llm_response(self, toy_repo, monkeypatch):
        monkeypatch.setattr(
            patcher.llm_providers, "generate_with_fallback",
            lambda *a, **k: ("not json at all", "openai"),
        )
        before = len(worktree.list_worktrees(repo_dir=str(toy_repo)))
        with pytest.raises(patcher.llm_json.LLMResponseParseError):
            patcher.generate_patch(str(toy_repo), _detection(), base_ref="main")
        after = len(worktree.list_worktrees(repo_dir=str(toy_repo)))
        assert after == before


class TestPromptSanitization:
    def test_secrets_in_current_file_content_are_redacted(self, toy_repo, monkeypatch):
        (toy_repo / "config_snippet.py").write_text("OPENAI_API_KEY = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n")
        git(toy_repo, "add", "config_snippet.py")
        git(toy_repo, "commit", "-q", "-m", "add config snippet")

        captured = {}

        def fake_generate(system_prompt, user_prompt, *, provider_name=None):
            captured["user_prompt"] = user_prompt
            return _llm_response(files={"config_snippet.py": "OPENAI_API_KEY = 'placeholder'\n"}), "openai"

        monkeypatch.setattr(patcher.llm_providers, "generate_with_fallback", fake_generate)
        detection = _detection(suggested_files=["config_snippet.py"])
        wt, _proposal = patcher.generate_patch(str(toy_repo), detection, base_ref="main")
        try:
            assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in captured["user_prompt"]
        finally:
            worktree.remove(wt)
