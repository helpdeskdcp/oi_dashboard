"""
test_agents/test_llm_providers.py -- regression tests for the multi-
provider LLM abstraction layer (agents/llm_providers/). Never makes a
real network call -- every generate() test injects a fake SDK module via
sys.modules so these tests are deterministic and offline, matching this
codebase's existing "synthetic data only, no live dependency" philosophy
(see test_exit_engine_v4.py's own docstring).
"""
import sys
import types

import pytest

from agents import config
from agents.llm_providers import LLMProviderError, available_providers, get_llm_provider
from agents.llm_providers.claude_provider import ClaudeProvider
from agents.llm_providers.gemini_provider import GeminiProvider
from agents.llm_providers.ollama_provider import OllamaProvider
from agents.llm_providers.openai_provider import OpenAIProvider


class TestAvailableProviders:
    def test_lists_all_four_required_providers(self):
        assert available_providers() == ["claude", "gemini", "ollama", "openai"]


class TestOpenAIProvider:
    def test_not_configured_without_api_key(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert OpenAIProvider().is_configured() is False

    def test_configured_with_api_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key-for-tests")
        assert OpenAIProvider().is_configured() is True

    def test_generate_calls_the_sdk_and_returns_content(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key-for-tests")

        captured = {}

        class _FakeMessage:
            content = "here is the patch"

        class _FakeChoice:
            message = _FakeMessage()

        class _FakeCompletions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return types.SimpleNamespace(choices=[_FakeChoice()])

        class _FakeChat:
            completions = _FakeCompletions()

        class _FakeOpenAI:
            def __init__(self, api_key):
                captured["api_key"] = api_key
                self.chat = _FakeChat()

        fake_module = types.SimpleNamespace(OpenAI=_FakeOpenAI)
        monkeypatch.setitem(sys.modules, "openai", fake_module)

        result = OpenAIProvider().generate("system prompt", "user prompt")
        assert result == "here is the patch"
        assert captured["api_key"] == "fake-key-for-tests"
        assert captured["messages"][0] == {"role": "system", "content": "system prompt"}
        assert captured["messages"][1] == {"role": "user", "content": "user prompt"}

    def test_generate_without_api_key_raises_llm_provider_error(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(LLMProviderError, match="OPENAI_API_KEY"):
            OpenAIProvider().generate("s", "u")

    def test_sdk_failure_wrapped_as_llm_provider_error(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key-for-tests")

        class _FailingCompletions:
            def create(self, **kwargs):
                raise RuntimeError("network exploded")

        class _FailingChat:
            completions = _FailingCompletions()

        class _FailingOpenAI:
            def __init__(self, api_key):
                self.chat = _FailingChat()

        monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=_FailingOpenAI))
        with pytest.raises(LLMProviderError, match="OpenAI request failed"):
            OpenAIProvider().generate("s", "u")


class TestClaudeProviderWithoutSdkInstalled:
    # The 'anthropic' package is not installed in this environment -- these
    # tests confirm that degrades to a clean is_configured()==False /
    # LLMProviderError rather than an ImportError leaking out.
    def test_not_configured_without_sdk_even_with_api_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")
        monkeypatch.setitem(sys.modules, "anthropic", None)   # force ImportError on `import anthropic`
        assert ClaudeProvider().is_configured() is False

    def test_generate_without_sdk_raises_llm_provider_error(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-tests")
        monkeypatch.setitem(sys.modules, "anthropic", None)
        with pytest.raises(LLMProviderError, match="anthropic"):
            ClaudeProvider().generate("s", "u")

    def test_not_configured_without_api_key(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        assert ClaudeProvider().is_configured() is False


class TestGeminiProviderWithoutSdkInstalled:
    def test_not_configured_without_sdk_even_with_api_key(self, monkeypatch):
        monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-tests")
        monkeypatch.setitem(sys.modules, "google.generativeai", None)
        assert GeminiProvider().is_configured() is False

    def test_not_configured_without_api_key(self, monkeypatch):
        monkeypatch.delenv("GEMINI_API_KEY", raising=False)
        assert GeminiProvider().is_configured() is False


class TestOllamaProvider:
    def test_not_configured_when_unreachable(self, monkeypatch):
        # Port 1 is reserved/unassignable -- guaranteed connection failure,
        # not dependent on whether an ollama server happens to be running
        # on the test machine.
        monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:1")
        assert OllamaProvider().is_configured() is False

    def test_generate_against_unreachable_host_raises_llm_provider_error(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:1")
        with pytest.raises(LLMProviderError, match="Ollama request failed"):
            OllamaProvider().generate("s", "u")

    def test_no_api_key_env_var_is_read(self):
        # Local-model backend -- configuration is reachability, not a key.
        import inspect

        source = inspect.getsource(OllamaProvider)
        assert "API_KEY" not in source


class TestGetLlmProvider:
    def test_unknown_provider_name_raises(self):
        with pytest.raises(LLMProviderError, match="unknown LLM provider"):
            get_llm_provider("not-a-real-provider")

    def test_unconfigured_known_provider_raises(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        with pytest.raises(LLMProviderError, match="not configured"):
            get_llm_provider("openai")

    def test_configured_provider_returns_an_adapter(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key-for-tests")
        provider = get_llm_provider("openai")
        assert provider.name == "openai"

    def test_no_name_reads_config_agent_llm_provider(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key-for-tests")
        monkeypatch.setattr(config, "AGENT_LLM_PROVIDER", "openai")
        provider = get_llm_provider()
        assert provider.name == "openai"

    def test_switching_provider_is_config_only_no_code_change_needed(self, monkeypatch):
        # The crux of the "switch providers via configuration only"
        # requirement: the SAME call, with only the config value changed,
        # resolves to a different concrete adapter class.
        monkeypatch.setenv("OPENAI_API_KEY", "fake-key-for-tests")
        monkeypatch.setattr(config, "AGENT_LLM_PROVIDER", "openai")
        assert type(get_llm_provider()).__name__ == "OpenAIProvider"

        monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11434")
        monkeypatch.setattr(config, "AGENT_LLM_PROVIDER", "ollama")
        # ollama may or may not actually be reachable in this environment --
        # what matters here is WHICH adapter class was selected, so patch
        # is_configured() to isolate that from network reachability.
        monkeypatch.setattr(OllamaProvider, "is_configured", lambda self: True)
        assert type(get_llm_provider()).__name__ == "OllamaProvider"
