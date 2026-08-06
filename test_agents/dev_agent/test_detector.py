"""
test_agents/dev_agent/test_detector.py -- regression tests for
agents/dev_agent/detector.py. Every LLM call is monkeypatched at
llm_providers.generate_with_fallback -- never a real network call, and
never a real subprocess -- matching this codebase's offline test
philosophy (see test_llm_providers.py's own docstring).
"""
import json

import pytest

from agents import config
from agents.dev_agent import detector, llm_json
from agents.memory.sqlite_store import SQLiteMemoryStore


def _write(tmp_path, rel_path, content):
    path = tmp_path / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


class TestSelfModificationGuard:
    def test_refuses_before_any_llm_call_when_target_touches_guarded_prefix(self, tmp_path, monkeypatch):
        def must_not_be_called(*args, **kwargs):
            raise AssertionError("detect() called the LLM despite a guarded target file")

        monkeypatch.setattr(detector.llm_providers, "generate_with_fallback", must_not_be_called)

        result = detector.detect(str(tmp_path), "suspicious trigger", ["agents/base_agent.py"])

        assert result.refused is True
        assert "agents/" in result.refusal_reason
        assert result.confidence_score == 0

    def test_refuses_for_exact_guard_prefix_directory(self, tmp_path, monkeypatch):
        def must_not_be_called(*args, **kwargs):
            raise AssertionError("detect() called the LLM despite a guarded target file")

        monkeypatch.setattr(detector.llm_providers, "generate_with_fallback", must_not_be_called)
        result = detector.detect(str(tmp_path), "trigger", ["agents/dev_agent/pipeline.py"])
        assert result.refused is True

    def test_does_not_refuse_a_lookalike_non_guarded_path(self, tmp_path, monkeypatch):
        # "agents_docs.py" shares a prefix character-wise with "agents/"
        # but is not actually under it -- must not false-positive.
        _write(tmp_path, "agents_docs.py", "# not actually guarded\n")
        monkeypatch.setattr(
            detector.llm_providers, "generate_with_fallback",
            lambda *a, **k: (json.dumps({"issue_summary": "ok", "root_cause": "ok", "confidence_score": 50}), "openai"),
        )
        result = detector.detect(str(tmp_path), "trigger", ["agents_docs.py"])
        assert result.refused is False


class TestDetectHappyPath:
    def test_parses_structured_llm_response(self, tmp_path, monkeypatch):
        _write(tmp_path, "backtest.py", "def compute(): pass\n")
        captured = {}

        def fake_generate(system_prompt, user_prompt, *, provider_name=None):
            captured["system_prompt"] = system_prompt
            captured["user_prompt"] = user_prompt
            captured["provider_name"] = provider_name
            return (
                json.dumps({
                    "issue_summary": "off-by-one in compute()",
                    "root_cause": "loop bound is wrong",
                    "confidence_score": 87,
                    "suggested_files": ["backtest.py"],
                }),
                "claude",
            )

        monkeypatch.setattr(detector.llm_providers, "generate_with_fallback", fake_generate)

        result = detector.detect(str(tmp_path), "test_backtest failed", ["backtest.py"], provider_name="claude")

        assert result.refused is False
        assert result.issue_summary == "off-by-one in compute()"
        assert result.root_cause == "loop bound is wrong"
        assert result.confidence_score == 87
        assert result.suggested_files == ["backtest.py"]
        assert result.provider_used == "claude"
        assert captured["provider_name"] == "claude"
        assert "def compute(): pass" in captured["user_prompt"]

    def test_falls_back_to_target_files_when_llm_omits_suggested_files(self, tmp_path, monkeypatch):
        _write(tmp_path, "backtest.py", "x = 1\n")
        monkeypatch.setattr(
            detector.llm_providers, "generate_with_fallback",
            lambda *a, **k: (json.dumps({"issue_summary": "s", "root_cause": "r", "confidence_score": 10}), "openai"),
        )
        result = detector.detect(str(tmp_path), "trigger", ["backtest.py"])
        assert result.suggested_files == ["backtest.py"]

    def test_malformed_json_response_degrades_gracefully(self, tmp_path, monkeypatch):
        _write(tmp_path, "backtest.py", "x = 1\n")
        monkeypatch.setattr(
            detector.llm_providers, "generate_with_fallback",
            lambda *a, **k: ("I looked at the code and it seems fine to me.", "openai"),
        )
        result = detector.detect(str(tmp_path), "trigger", ["backtest.py"])
        assert result.refused is False
        assert result.issue_summary == "I looked at the code and it seems fine to me."
        assert result.confidence_score == 0
        assert result.suggested_files == ["backtest.py"]

    def test_confidence_score_is_clamped_to_0_100(self, tmp_path, monkeypatch):
        _write(tmp_path, "backtest.py", "x = 1\n")
        monkeypatch.setattr(
            detector.llm_providers, "generate_with_fallback",
            lambda *a, **k: (json.dumps({"issue_summary": "s", "root_cause": "r", "confidence_score": 500}), "openai"),
        )
        result = detector.detect(str(tmp_path), "trigger", ["backtest.py"])
        assert result.confidence_score == 100

    def test_nonnumeric_confidence_score_defaults_to_zero(self, tmp_path, monkeypatch):
        _write(tmp_path, "backtest.py", "x = 1\n")
        monkeypatch.setattr(
            detector.llm_providers, "generate_with_fallback",
            lambda *a, **k: (json.dumps({"issue_summary": "s", "root_cause": "r", "confidence_score": "high"}), "openai"),
        )
        result = detector.detect(str(tmp_path), "trigger", ["backtest.py"])
        assert result.confidence_score == 0


class TestPromptSanitization:
    def test_secrets_in_file_content_are_redacted_before_reaching_the_llm(self, tmp_path, monkeypatch):
        _write(tmp_path, "config_snippet.py", "OPENAI_API_KEY = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n")
        captured = {}

        def fake_generate(system_prompt, user_prompt, *, provider_name=None):
            captured["user_prompt"] = user_prompt
            return (json.dumps({"issue_summary": "s", "root_cause": "r", "confidence_score": 1}), "openai")

        monkeypatch.setattr(detector.llm_providers, "generate_with_fallback", fake_generate)
        detector.detect(str(tmp_path), "trigger", ["config_snippet.py"])

        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in captured["user_prompt"]

    def test_secrets_in_trigger_text_are_redacted(self, tmp_path, monkeypatch):
        _write(tmp_path, "backtest.py", "x = 1\n")
        captured = {}

        def fake_generate(system_prompt, user_prompt, *, provider_name=None):
            captured["user_prompt"] = user_prompt
            return (json.dumps({"issue_summary": "s", "root_cause": "r", "confidence_score": 1}), "openai")

        monkeypatch.setattr(detector.llm_providers, "generate_with_fallback", fake_generate)
        detector.detect(str(tmp_path), "failure reported by user helpdeskdcp@gmail.com", ["backtest.py"])

        assert "helpdeskdcp@gmail.com" not in captured["user_prompt"]


class TestMissingFile:
    def test_missing_target_file_reads_as_empty_string_not_a_crash(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            detector.llm_providers, "generate_with_fallback",
            lambda *a, **k: (json.dumps({"issue_summary": "s", "root_cause": "r", "confidence_score": 1}), "openai"),
        )
        result = detector.detect(str(tmp_path), "trigger", ["does_not_exist.py"])
        assert result.refused is False


class TestMemorySearchBeforeGenerating:
    # Requirement: "Every AI proposal must search this memory before
    # generating code." These tests use a real SQLiteMemoryStore (never
    # the repo's real oi_history.db -- always a tmp_path file) so they
    # confirm an actual search happened and its results actually reached
    # the LLM prompt, not just that a mock was invoked.

    def test_relevant_memory_is_spliced_into_the_prompt(self, tmp_path, monkeypatch):
        _write(tmp_path, "backtest.py", "def compute(): pass\n")
        store = SQLiteMemoryStore(db_path=str(tmp_path / "mem.db"))
        store.record_bug_fix(
            trigger="prior trigger", issue_summary="off-by-one in compute()",
            root_cause="loop bound wrong", fix_summary="fixed the range() call",
            target_files=["backtest.py"],
        )
        captured = {}

        def fake_generate(system_prompt, user_prompt, *, provider_name=None):
            captured["user_prompt"] = user_prompt
            return (json.dumps({"issue_summary": "s", "root_cause": "r", "confidence_score": 1}), "openai")

        monkeypatch.setattr(detector.llm_providers, "generate_with_fallback", fake_generate)
        detector.detect(str(tmp_path), "test failing on compute()", ["backtest.py"], memory_store=store)

        assert "off-by-one in compute()" in captured["user_prompt"]
        assert "fixed the range() call" in captured["user_prompt"]

    def test_no_history_still_produces_a_placeholder_context(self, tmp_path, monkeypatch):
        _write(tmp_path, "backtest.py", "x = 1\n")
        store = SQLiteMemoryStore(db_path=str(tmp_path / "mem.db"))
        captured = {}

        def fake_generate(system_prompt, user_prompt, *, provider_name=None):
            captured["user_prompt"] = user_prompt
            return (json.dumps({"issue_summary": "s", "root_cause": "r", "confidence_score": 1}), "openai")

        monkeypatch.setattr(detector.llm_providers, "generate_with_fallback", fake_generate)
        detector.detect(str(tmp_path), "trigger", ["backtest.py"], memory_store=store)

        assert "No relevant history" in captured["user_prompt"]

    def test_default_memory_store_is_used_when_none_injected(self, tmp_path, monkeypatch):
        # No memory_store passed -- detect() must fall back to
        # agents.memory.get_memory_store(), not skip the search entirely.
        _write(tmp_path, "backtest.py", "x = 1\n")
        monkeypatch.setattr(config, "MEMORY_DB_PATH", str(tmp_path / "fallback_mem.db"))
        called = {}

        def fake_generate(system_prompt, user_prompt, *, provider_name=None):
            called["ran"] = True
            return (json.dumps({"issue_summary": "s", "root_cause": "r", "confidence_score": 1}), "openai")

        monkeypatch.setattr(detector.llm_providers, "generate_with_fallback", fake_generate)
        detector.detect(str(tmp_path), "trigger", ["backtest.py"])

        assert called.get("ran") is True
        import os
        assert os.path.exists(str(tmp_path / "fallback_mem.db"))
