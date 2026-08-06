"""
agents/dev_agent/llm_json.py -- parses a JSON object out of raw LLM text.
Models routinely wrap JSON in ```json ... ``` fences, or add a sentence
of prose before/after it, despite an explicit "respond with JSON only"
instruction. This is the one place that leniency lives, so detector.py
and patcher.py can both assume parse_object() either returns a dict or
raises -- no LLM-specific parsing logic duplicated in either of them.
"""
import json
import re

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class LLMResponseParseError(Exception):
    pass


def parse_object(text: str) -> dict:
    """Tries, in order: a ```json fenced block, the whole text as-is, and
    the substring between the first "{" and the last "}". Returns the
    first candidate that parses to a JSON object (not a list/scalar).
    Raises LLMResponseParseError if none of them do."""
    candidates = []

    fence_match = _FENCE_RE.search(text or "")
    if fence_match:
        candidates.append(fence_match.group(1))

    candidates.append(text or "")

    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1])

    for candidate in candidates:
        try:
            obj = json.loads(candidate.strip())
        except (json.JSONDecodeError, ValueError, AttributeError):
            continue
        if isinstance(obj, dict):
            return obj

    raise LLMResponseParseError(f"could not parse a JSON object out of LLM response: {(text or '')[:200]!r}")
