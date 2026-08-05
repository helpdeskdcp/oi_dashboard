"""
agents/llm_providers/ -- the multi-provider LLM abstraction layer.
Requirement: "Do not hardcode any provider... must be able to switch
providers via configuration only." Adding a new provider (including a
future local model beyond Ollama) means one new adapter module here plus
one line in _LOADERS -- agents.config.AGENT_LLM_PROVIDER and every caller
of get_llm_provider() are unaffected.

Each _load_* function only imports its adapter module (and therefore its
SDK, if any) when that provider is actually selected -- choosing "openai"
never requires anthropic/google-generativeai to be installed, and vice
versa.
"""
from .. import config
from .base import LLMProvider, LLMProviderError


def _load_openai():
    from .openai_provider import OpenAIProvider

    return OpenAIProvider()


def _load_claude():
    from .claude_provider import ClaudeProvider

    return ClaudeProvider()


def _load_ollama():
    from .ollama_provider import OllamaProvider

    return OllamaProvider()


def _load_gemini():
    from .gemini_provider import GeminiProvider

    return GeminiProvider()


_LOADERS = {
    "openai": _load_openai,
    "claude": _load_claude,
    "ollama": _load_ollama,
    "gemini": _load_gemini,
}


def available_providers() -> list[str]:
    return sorted(_LOADERS)


def get_llm_provider(name: str | None = None) -> LLMProvider:
    """name=None (default) reads agents.config.AGENT_LLM_PROVIDER -- the
    ONLY place provider selection is decided; nothing else in this
    codebase should read that config value directly. Raises
    LLMProviderError (not KeyError) for both an unknown name and a known-
    but-unconfigured provider, so callers have exactly one exception type
    to handle either way."""
    provider_name = name or config.AGENT_LLM_PROVIDER
    if provider_name not in _LOADERS:
        raise LLMProviderError(
            f"unknown LLM provider {provider_name!r} -- available: {available_providers()}"
        )
    provider = _LOADERS[provider_name]()
    if not provider.is_configured():
        raise LLMProviderError(
            f"LLM provider {provider_name!r} is selected (AGENT_LLM_PROVIDER) but not configured "
            f"-- check its required environment variable(s) and that its SDK is installed."
        )
    return provider
