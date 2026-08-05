"""
agents/llm_providers/base.py -- the one interface every LLM backend must
implement. agents/dev_agent/patcher.py (Milestone 3) only ever calls
agents.llm_providers.get_llm_provider() and talks to the LLMProvider it
gets back -- it never imports openai/anthropic/google.generativeai
directly, and never branches on which provider is active. Provider
selection is agents.config.AGENT_LLM_PROVIDER, config-only, per the
"switch providers via configuration only" requirement.
"""
import abc


class LLMProviderError(Exception):
    """Raised by every adapter, for every kind of failure (missing API
    key, SDK not installed, network error, non-2xx response). Callers
    catch this ONE exception type regardless of which concrete provider
    is active -- they never need to know that OpenAIProvider raises for a
    different underlying reason than OllamaProvider does."""


class LLMProvider(abc.ABC):
    name: str = "base"

    @abc.abstractmethod
    def generate(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 4096) -> str:
        """Returns the model's text response. Raises LLMProviderError on
        any failure -- never returns None or an empty string to signal
        failure. Per the architecture doc's safety layer ("no evidence,
        no Finding"), a caller must be able to tell a genuine (if
        unhelpful) empty response apart from a failed request, and
        silently returning "" for both would erase that distinction."""
        raise NotImplementedError

    @abc.abstractmethod
    def is_configured(self) -> bool:
        """True if this provider has what it needs (an API key, a
        reachable local endpoint, an installed SDK) to actually run
        generate(). Checked by get_llm_provider() before returning an
        adapter, so a misconfigured provider fails fast and loud at
        selection time, not on the first real patch attempt."""
        raise NotImplementedError
