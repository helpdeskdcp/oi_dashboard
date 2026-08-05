"""
agents/llm_providers/gemini_provider.py -- Google Gemini adapter. The
'google-generativeai' package is not installed in this environment as of
this writing -- selecting this provider raises a clear LLMProviderError
at get_llm_provider() time rather than failing on the first patch
attempt (same pattern as claude_provider.py).
"""
import os

from .base import LLMProvider, LLMProviderError


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(self):
        self.api_key = os.getenv("GEMINI_API_KEY", "")
        self.model = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

    def is_configured(self) -> bool:
        if not self.api_key:
            return False
        try:
            import google.generativeai  # noqa: F401
        except ImportError:
            return False
        return True

    def generate(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 4096) -> str:
        if not self.api_key:
            raise LLMProviderError("GEMINI_API_KEY is not set")
        try:
            import google.generativeai as genai
        except ImportError as e:
            raise LLMProviderError("the 'google-generativeai' package is not installed") from e
        genai.configure(api_key=self.api_key)
        try:
            model = genai.GenerativeModel(self.model, system_instruction=system_prompt)
            resp = model.generate_content(
                user_prompt, generation_config={"max_output_tokens": max_tokens}
            )
        except Exception as e:
            raise LLMProviderError(f"Gemini request failed: {e}") from e
        return resp.text or ""
