"""
agents/llm_providers/ollama_provider.py -- local Ollama adapter. No API
key -- is_configured() checks that a local Ollama server is actually
reachable, since that's the only real "is this usable" signal for a
local-model backend. Uses only the standard library (urllib) rather than
adding an HTTP client dependency for a single-purpose local call.
"""
import json
import os
import urllib.error
import urllib.request

from .base import LLMProvider, LLMProviderError

DEFAULT_HOST = "http://localhost:11434"


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self):
        self.host = os.getenv("OLLAMA_HOST", DEFAULT_HOST)
        self.model = os.getenv("OLLAMA_MODEL", "llama3")

    def is_configured(self) -> bool:
        try:
            urllib.request.urlopen(f"{self.host}/api/tags", timeout=2)
            return True
        except (urllib.error.URLError, OSError):
            return False

    def generate(self, system_prompt: str, user_prompt: str, *, max_tokens: int = 4096) -> str:
        body = json.dumps(
            {
                "model": self.model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "options": {"num_predict": max_tokens},
            }
        ).encode()
        req = urllib.request.Request(
            f"{self.host}/api/chat", data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            raise LLMProviderError(f"Ollama request failed: {e}") from e
        return data.get("message", {}).get("content", "")
