"""
agents/dev_agent/patcher.py -- Milestone 3's LLM-calling patch generator.
Pipeline step 3: "Generate a patch inside it (LLM)."

LLM permissions exercised here, and only these: generate a patch,
generate tests, generate documentation. The model is never allowed to run
a shell command or touch a file itself -- every file this module writes
is written by ordinary Python open(...).write() calls, inside an
isolated worktree worktree.create() just made for this proposal alone.
The LLM only ever returns text; this module parses that text as a JSON
object and, if and only if it passes every check below, applies it as
plain file writes. There is no eval/exec of anything the LLM returns, and
no subprocess call built from LLM output -- the only subprocess calls in
this module are the two fixed `git add` / `git commit` invocations this
code itself constructs.

Guarded three times, independently, against a self-modification attempt:
1. detector.detect() already refused before a DetectionResult with a
   guarded target_files list would ever reach this module.
2. Before requesting a patch, again, against detection.suggested_files --
   in case a caller invokes generate_patch() directly with a hand-built
   DetectionResult that skipped step 1.
3. After parsing the LLM's response, against every path the model itself
   proposed writing to -- the same "never trust upstream intent, always
   re-verify the actual artifact" principle gates/backtest_compare.py's
   diff re-check already uses. A model can be prompted around; a file
   path it actually tries to write cannot.
Any of the three raises detector.SelfModificationRefused. The third case
also removes the worktree this function itself created before raising --
no partially-written worktree is ever left behind for a caller to
accidentally validate or merge.

Milestone 4 requirement: "Every AI proposal must search this memory
before generating code." generate_patch() searches agents.memory (past
bug fixes, failed experiments, and -- when a strategy file is involved --
known parameter sets and prior strategy-evolution entries) before
building the patch prompt, so the model sees what's already been tried
on this file before it writes a single line.
"""
import dataclasses
import os
import subprocess
from typing import Optional

from .. import config, llm_providers, memory
from ..memory import context as memory_context
from . import llm_json, sanitizer, worktree
from .detector import DetectionResult, SelfModificationRefused
from .patch_generator import touches_guarded_path

SYSTEM_PROMPT = (
    "You are the AI Developer agent's patch-generation engine for BATI, an "
    "options-trading dashboard. You may generate code patches, generate "
    "tests, and generate documentation updates -- you have no ability to "
    "execute commands, and nothing you write is applied to production "
    "unless it passes every validation gate and a human approves it. "
    "Respond with a single JSON object and nothing else, with exactly "
    'these keys: "files" (object mapping repo-relative path -> full new '
    "file content, for the source file(s) you are changing), \"tests\" "
    "(object mapping repo-relative path -> full new file content, for "
    "test files you are adding or updating -- always include at least "
    'one), "docs" (object mapping repo-relative path -> full new file '
    "content for any documentation you are updating; may be an empty "
    'object), "rationale" (why this change fixes the issue), '
    '"expected_impact" (what should measurably change), "risk_assessment" '
    '(what could go wrong), "confidence_score" (integer 0-100, your '
    "confidence this patch is correct and safe). Never propose writing to "
    "any path under 'agents/' -- that is a hard restriction with no "
    "exception. File content must be file content only -- never a shell "
    "command or a command to be executed."
)


@dataclasses.dataclass
class PatchProposal:
    rationale: str
    expected_impact: str
    risk_assessment: str
    confidence_score: int
    files_written: list
    tests_written: list
    docs_written: list
    provider_used: str


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


def _infer_strategy_name(files: list) -> Optional[str]:
    """Best-effort strategy identifier for memory lookups: the basename
    (minus extension) of the first suggested file, e.g. "exit_engine_v4.py"
    -> "exit_engine_v4". None if there's nothing to infer from -- callers
    treat that as "no strategy-specific memory to search"."""
    if not files:
        return None
    base = os.path.basename(files[0])
    name, _ext = os.path.splitext(base)
    return name or None


def _build_user_prompt(detection: DetectionResult, file_contents: dict, memory_text: str) -> str:
    code_block = "\n\n".join(f"--- {path} ---\n{content}" for path, content in file_contents.items())
    return (
        f"Relevant memory (search this before proposing anything new):\n{memory_text}\n\n"
        f"Issue summary: {sanitizer.sanitize(detection.issue_summary)}\n\n"
        f"Root cause: {sanitizer.sanitize(detection.root_cause)}\n\n"
        f"Current file contents:\n{code_block}\n\n"
        "Generate the fix as the JSON object described in your instructions."
    )


def _write_files(worktree_path: str, files: dict) -> list:
    written = []
    for rel_path, content in (files or {}).items():
        if not isinstance(rel_path, str) or not isinstance(content, str):
            continue
        full_path = os.path.join(worktree_path, rel_path)
        os.makedirs(os.path.dirname(full_path) or worktree_path, exist_ok=True)
        with open(full_path, "w") as fh:
            fh.write(content)
        written.append(rel_path)
    return written


def _commit_all(worktree_path: str, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=worktree_path, capture_output=True, text=True, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", message], cwd=worktree_path, capture_output=True, text=True, check=True
    )


def _coerce_confidence(value) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


def generate_patch(repo_dir: str, detection: DetectionResult, *, base_ref: str = "main",
                    provider_name: Optional[str] = None, memory_store=None):
    """Returns (Worktree, PatchProposal) on success. Raises
    SelfModificationRefused, llm_json.LLMResponseParseError, ValueError
    (empty response), or subprocess.CalledProcessError (git failure) on
    any failure -- in every case except guard #2 below, the worktree this
    function created has already been rolled back before the exception
    propagates, so a caller never inherits a half-written worktree.
    memory_store injects a MemoryStore for tests; production callers
    leave it None and get agents.memory.get_memory_store()."""
    guard = config.SELF_MODIFICATION_GUARD_PREFIX
    if touches_guarded_path(detection.suggested_files, guard):
        raise SelfModificationRefused(
            f"detection.suggested_files touch the guarded prefix {guard!r} -- "
            f"refused before any worktree was created."
        )

    store = memory_store or memory.get_memory_store()
    strategy_name = _infer_strategy_name(detection.suggested_files)
    memory_text = sanitizer.sanitize(
        memory_context.build_context(
            store, trigger=detection.trigger, target_files=detection.suggested_files,
            strategy_name=strategy_name,
        )
    )

    wt = worktree.create(detection.trigger or "patch", repo_dir=repo_dir, base_ref=base_ref)
    try:
        file_contents = sanitizer.sanitize_files(_read_files(wt.path, detection.suggested_files))
        user_prompt = _build_user_prompt(detection, file_contents, memory_text)

        text, provider_used = llm_providers.generate_with_fallback(
            SYSTEM_PROMPT, user_prompt, provider_name=provider_name
        )
        parsed = llm_json.parse_object(text)

        proposed_paths = list((parsed.get("files") or {}).keys()) + \
            list((parsed.get("tests") or {}).keys()) + list((parsed.get("docs") or {}).keys())
        if touches_guarded_path(proposed_paths, guard):
            raise SelfModificationRefused(
                f"LLM response proposed writing under the guarded prefix {guard!r} -- "
                f"refused, worktree discarded."
            )

        files_written = _write_files(wt.path, parsed.get("files"))
        tests_written = _write_files(wt.path, parsed.get("tests"))
        docs_written = _write_files(wt.path, parsed.get("docs"))

        if not (files_written or tests_written or docs_written):
            raise ValueError("LLM response contained no file/test/doc content to write")

        _commit_all(wt.path, f"agent: {(detection.issue_summary or 'automated patch')[:72]}")

        proposal = PatchProposal(
            rationale=str(parsed.get("rationale") or "")[:4000],
            expected_impact=str(parsed.get("expected_impact") or "")[:2000],
            risk_assessment=str(parsed.get("risk_assessment") or "")[:2000],
            confidence_score=_coerce_confidence(parsed.get("confidence_score")),
            files_written=files_written, tests_written=tests_written, docs_written=docs_written,
            provider_used=provider_used,
        )
        return wt, proposal
    except Exception:
        worktree.remove(wt)
        raise
