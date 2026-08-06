"""
agents/dev_agent/patch_generator.py -- the only way a proposal's code
change leaves its worktree: a text diff, never a direct write to the
checkout a human is using. `git diff <base_ref>...<branch>` inside the
worktree is exactly what a human reviewer would run before merging.
"""
import subprocess


class PatchGenerationError(Exception):
    pass


def generate(worktree_path: str, base_ref: str, branch: str) -> str:
    """Returns the unified diff of `branch` against its merge-base with
    base_ref (three-dot range -- unrelated concurrent changes on
    base_ref don't pollute the patch)."""
    result = subprocess.run(
        ["git", "diff", f"{base_ref}...{branch}"],
        cwd=worktree_path, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise PatchGenerationError(f"git diff failed: {result.stderr.strip()}")
    return result.stdout


def changed_files(worktree_path: str, base_ref: str, branch: str) -> list:
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base_ref}...{branch}"],
        cwd=worktree_path, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise PatchGenerationError(f"git diff --name-only failed: {result.stderr.strip()}")
    return [f for f in result.stdout.splitlines() if f]


def touches_guarded_path(files: list, guard_prefix: str) -> bool:
    """Milestone 3's detector calls the equivalent check FIRST, before a
    worktree even exists (the self-modification refusal). patch_generator
    exposes the same check so the pipeline can verify the actual
    committed diff too -- not just the pre-check on the intended target
    -- before ever proposing a change."""
    stripped = guard_prefix.rstrip("/")
    return any(f == stripped or f.startswith(guard_prefix) for f in files)
