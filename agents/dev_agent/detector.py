"""
agents/dev_agent/detector.py -- Milestone 3's LLM-assisted issue scan.
Pipeline step 1: "Detect a candidate issue (LLM-assisted)."

LLM permissions exercised here, and only these: read code (via the file
contents this module puts in the prompt), analyze code, explain a bug.
This module never writes a file, never runs a shell command, and never
calls git -- it only reads target_files off disk and asks the configured
LLM provider (with automatic fallback -- see agents.llm_providers) to
explain what's wrong and how confident it is that there's a real,
fixable issue.

The agents/** self-modification refusal is enforced FIRST, before any
file is read or any prompt is built -- per AI_DEVELOPER_AGENT_PLAN.md's
"detector.py: candidate-issue scan; enforces the agents/** refusal
FIRST." patcher.py and pipeline.py each re-check the same guard
independently against what actually gets produced (defense in depth,
the same principle gates/backtest_compare.py's diff re-check already
uses), but this is the earliest point in the whole framework a refusal
can happen -- before a worktree even exists, before a single byte of a
guarded file is read into a prompt.
"""
import dataclasses
import os
from typing import Optional

from .. import config, llm_providers
from . import llm_json, sanitizer
from .patch_generator import touches_guarded_path

SYSTEM_PROMPT = (
    "You are the AI Developer agent's analysis engine for BATI, an options-"
    "trading dashboard. You may ONLY read, analyze, and explain code -- you "
    "have no ability to execute commands, modify files, or commit anything, "
    "and nothing you say is applied automatically. Respond with a single "
    "JSON object and nothing else, with exactly these keys: "
    '"issue_summary" (one paragraph describing the problem), "root_cause" '
    '(one paragraph explaining why it happens), "confidence_score" (integer '
    "0-100, your confidence that this is a real, fixable issue), "
    '"suggested_files" (a JSON array of repo-relative file paths that would '
    "need to change to fix it)."
)


class SelfModificationRefused(Exception):
    """Raised nowhere in this module today -- detect() returns a refused
    DetectionResult instead of raising, so a caller doesn't need a
    try/except just to notice a self-modification attempt. Defined here
    (rather than only in patcher.py) so both modules share one exception
    type for the same guard, and pipeline.py can catch it from either."""


@dataclasses.dataclass
class DetectionResult:
    trigger: str
    target_files: list
    issue_summary: str
    root_cause: str
    confidence_score: int
    suggested_files: list
    provider_used: str
    refused: bool = False
    refusal_reason: Optional[str] = None


def _read_files(repo_dir: str, files: list) -> dict:
    contents = {}
    for f in files:
        path = os.path.join(repo_dir, f)
        try:
            with open(path, "r", errors="replace") as fh:
                contents[f] = fh.read()
        except OSError:
            contents[f] = ""
    return contents


def _coerce_confidence(value) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


def detect(repo_dir: str, trigger: str, target_files: list, *, provider_name: Optional[str] = None) -> DetectionResult:
    """trigger is free text describing why this scan is happening (a
    failing test's output, a bug report, a performance-regression note --
    whatever agents.config.DEV_AGENT_TRIGGERS names). target_files is the
    set of repo-relative paths to read and analyze."""
    guard = config.SELF_MODIFICATION_GUARD_PREFIX
    if touches_guarded_path(target_files, guard):
        return DetectionResult(
            trigger=trigger, target_files=target_files, issue_summary="", root_cause="",
            confidence_score=0, suggested_files=[], provider_used="", refused=True,
            refusal_reason=(
                f"target_files touch the self-modification-guarded prefix {guard!r} -- "
                f"refused before reading any file content or calling an LLM."
            ),
        )

    file_contents = sanitizer.sanitize_files(_read_files(repo_dir, target_files))
    code_block = "\n\n".join(f"--- {path} ---\n{content}" for path, content in file_contents.items())
    user_prompt = (
        f"Trigger: {sanitizer.sanitize(trigger)}\n\n"
        f"Files:\n{code_block}\n\n"
        "Analyze the above and respond with the JSON object described in your instructions."
    )

    text, provider_used = llm_providers.generate_with_fallback(
        SYSTEM_PROMPT, user_prompt, provider_name=provider_name
    )
    try:
        parsed = llm_json.parse_object(text)
    except llm_json.LLMResponseParseError:
        parsed = {}

    suggested = [f for f in parsed.get("suggested_files", []) if isinstance(f, str)]
    return DetectionResult(
        trigger=trigger, target_files=target_files,
        issue_summary=str(parsed.get("issue_summary") or text)[:4000],
        root_cause=str(parsed.get("root_cause") or "")[:4000],
        confidence_score=_coerce_confidence(parsed.get("confidence_score")),
        suggested_files=suggested or list(target_files),
        provider_used=provider_used,
    )
