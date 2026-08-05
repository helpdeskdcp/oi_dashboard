"""
agents/llm_providers/openai_provider.py -- OpenAI adapter, the same
OPENAI_API_KEY / OPENAI_MODEL environment variables advisory_chatbot.py
already uses elsewhere in this codebase (see its CHATBOT_ENABLED
pattern) -- reusing a proven integration point rather than adding a
second convention for the same provider.
"""
import os

from .base import LLMProvider, LLMProviderError


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self):
        self.api_key = os.getenv("OPENAI_API_KEY", "")
        self.model = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def generate(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 4096) -> str:
        if not self.api_key:
            raise LLMProviderError("OPENAI_API_KEY is not set")
        try:
            import openai
        except ImportError as e:
            raise LLMProviderError("the 'openai' package is not installed") from e
        client = openai.OpenAI(api_key=self.api_key)
        try:
            resp = client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_tokens,
            )
        except Exception as e:
            raise LLMProviderError(f"OpenAI request failed: {e}") from e
        return resp.choices[0].message.content or ""
