"""
agents/dev_agent/sanitizer.py -- strips secrets/credentials/PII out of
any text before it is sent to an LLM provider. Requirement: "Prompt
sanitization. Remove: API keys, Secrets, Tokens, Credentials, Personal
information." detector.py and patcher.py both call sanitize()/
sanitize_files() on every piece of trigger text and file content before
it ever reaches llm_providers.generate_with_fallback() -- there is no
code path in agents/dev_agent/ that hands an LLM provider unsanitized
text.

Pattern-based, not exhaustive -- this is a best-effort redaction layer,
not a cryptographic guarantee that no secret ever reaches a third-party
API. Patterns are ordered specific-before-generic so a matched provider
key isn't left partially exposed by a later, broader pattern.

Phone-number matching is deliberately narrow (dash/space-separated
groups only, never dot-separated) because this codebase's source files
are full of legitimate decimal numeric literals (strike prices, P&L
thresholds); a loose phone regex would redact code, not PII.
"""
import re

REDACTED = "[REDACTED]"

_PATTERNS = (
    ("openai_key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9\-_]{20,}")),
    ("aws_access_key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9\-._~+/]{15,}=*")),
    (
        "generic_secret_assignment",
        re.compile(
            r"(?i)\b[A-Z0-9_]*(?:API[_-]?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PWD|"
            r"CREDENTIAL|ACCESS[_-]?KEY|CLIENT[_-]?SECRET)[A-Z0-9_]*\s*[:=]\s*"
            r"['\"]?[^\s'\"]{4,}['\"]?"
        ),
    ),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("phone", re.compile(r"\+?\d{1,3}[-\s]\d{3,5}[-\s]\d{3,5}(?:[-\s]\d{2,4})?\b")),
)


def sanitize(text: str) -> str:
    """Returns text with every matched pattern replaced by REDACTED.
    None-safe / empty-safe -- returns the input unchanged rather than
    raising, since callers pass this straight into an f-string prompt."""
    if not text:
        return text
    result = text
    for _name, pattern in _PATTERNS:
        result = pattern.sub(REDACTED, result)
    return result


def sanitize_files(file_contents: dict) -> dict:
    """Same as sanitize(), applied to every value in a {path: content}
    mapping -- the shape detector.py/patcher.py read source files into
    before building a prompt."""
    return {path: sanitize(content) for path, content in file_contents.items()}
