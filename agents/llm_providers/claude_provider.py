"""
agents/llm_providers/claude_provider.py -- Anthropic adapter. The
'anthropic' package is not installed in this environment as of this
writing (see is_configured()'s ImportError handling below) -- selecting
this provider raises a clear LLMProviderError at get_llm_provider() time
rather than failing on the first patch attempt.
"""
import os

from .base import LLMProvider, LLMProviderError


class ClaudeProvider(LLMProvider):
    name = "claude"

    def __init__(self):
        self.api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.model = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5")

    def is_configured(self) -> bool:
        if not self.api_key:
            return False
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False
        return True

    def generate(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 4096) -> str:
        if not self.api_key:
            raise LLMProviderError("ANTHROPIC_API_KEY is not set")
        try:
            import anthropic
        except ImportError as e:
            raise LLMProviderError("the 'anthropic' package is not installed") from e
        client = anthropic.Anthropic(api_key=self.api_key)
        try:
            resp = client.messages.create(
                model=self.model, max_tokens=max_tokens, system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception as e:
            raise LLMProviderError(f"Claude request failed: {e}") from e
        return "".join(block.text for block in resp.content if hasattr(block, "text"))
