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


def generate_with_fallback(
    system_prompt: str, user_prompt: str, *, max_tokens: int = 4096, provider_name: str | None = None
) -> tuple[str, str]:
    """"Automatic provider fallback" -- tries provider_name (or
    config.AGENT_LLM_PROVIDER) first, then config.AGENT_LLM_FALLBACK_ORDER
    in order, skipping whichever provider was already tried. Returns
    (response_text, provider_name_used) so a caller can record which
    provider actually served the request. Raises LLMProviderError, with
    every candidate's failure reason folded in, only once every provider
    has failed or is unconfigured -- callers never need to catch a
    per-provider exception type, same as get_llm_provider()."""
    primary = provider_name or config.AGENT_LLM_PROVIDER
    order = [primary] + [p for p in config.AGENT_LLM_FALLBACK_ORDER if p != primary]

    errors = []
    for candidate in order:
        if candidate not in _LOADERS:
            errors.append(f"{candidate}: unknown provider")
            continue
        try:
            provider = _LOADERS[candidate]()
        except Exception as e:  # noqa: BLE001 -- adapter construction is untrusted, must not abort the fallback chain
            errors.append(f"{candidate}: failed to load adapter ({e})")
            continue
        if not provider.is_configured():
            errors.append(f"{candidate}: not configured")
            continue
        try:
            text = provider.generate(system_prompt, user_prompt, max_tokens=max_tokens)
            return text, candidate
        except LLMProviderError as e:
            errors.append(f"{candidate}: {e}")
            continue

    raise LLMProviderError("every LLM provider failed or was unconfigured -- " + "; ".join(errors))


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
