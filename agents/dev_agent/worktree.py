"""
agents/dev_agent/worktree.py -- git worktree isolation for the AI
Developer agent. Every candidate change is validated inside a worktree
created here; nothing in this module ever runs `git commit`, `git
merge`, or `git checkout <branch>` against the checkout a human has open.
Multiple worktrees can coexist (one per in-flight proposal) -- create()
and checkout_existing() each mint a unique directory under
worktrees_root, so parallel pipeline runs never collide.
"""
import dataclasses
import os
import re
import subprocess
import time
import uuid

DEFAULT_WORKTREES_ROOT = ".dev_agent_worktrees"


class WorktreeError(Exception):
    pass


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str) -> str:
    slug = _SLUG_RE.sub("-", text.lower()).strip("-")
    return slug[:40] or "change"


@dataclasses.dataclass
class Worktree:
    path: str
    branch: str
    base_ref: str
    repo_dir: str


def create(slug: str, *, repo_dir: str = ".", base_ref: str = "main",
           worktrees_root: str = DEFAULT_WORKTREES_ROOT) -> Worktree:
    """Creates a NEW branch `agent/dev-<ts>-<slug>-<rand>` off base_ref,
    checked out into a fresh directory under worktrees_root. base_ref
    itself is never touched -- `git worktree add -b` only ever reads it
    as a starting point. Worktree.path is always absolute, so callers
    can pass it as a subprocess cwd regardless of their own cwd."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    branch = f"agent/dev-{ts}-{_slugify(slug)}-{uuid.uuid4().hex[:6]}"
    path = os.path.join(os.path.abspath(repo_dir), worktrees_root, branch.replace("/", "-"))
    result = subprocess.run(
        ["git", "worktree", "add", "-b", branch, path, base_ref],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise WorktreeError(f"git worktree add failed: {result.stderr.strip()}")
    return Worktree(path=path, branch=branch, base_ref=base_ref, repo_dir=repo_dir)


def checkout_existing(branch: str, *, repo_dir: str = ".",
                       worktrees_root: str = DEFAULT_WORKTREES_ROOT) -> Worktree:
    """Same isolation as create(), but for a branch that already exists
    (a candidate a human already committed today; the detector/patcher's
    commit starting Milestone 3). Never creates a new branch -- attaches
    a worktree to `branch` as-is. Worktree.path is always absolute."""
    path = os.path.join(os.path.abspath(repo_dir), worktrees_root, f"{_slugify(branch)}-{uuid.uuid4().hex[:6]}")
    result = subprocess.run(
        ["git", "worktree", "add", path, branch],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise WorktreeError(f"git worktree add failed: {result.stderr.strip()}")
    return Worktree(path=path, branch=branch, base_ref=branch, repo_dir=repo_dir)


def remove(wt: Worktree, *, force: bool = True) -> None:
    """Removes the worktree directory and deletes its branch. Idempotent
    -- a worktree that's already gone is not an error, since failure
    recovery calls this unconditionally on every exit path, including
    ones where the worktree may have already been cleaned up."""
    args = ["git", "worktree", "remove"]
    if force:
        args.append("--force")
    args.append(wt.path)
    result = subprocess.run(args, cwd=wt.repo_dir, capture_output=True, text=True)
    if result.returncode != 0 and "is not a working tree" not in result.stderr \
            and "not a valid path" not in result.stderr:
        raise WorktreeError(f"git worktree remove failed: {result.stderr.strip()}")
    subprocess.run(["git", "branch", "-D", wt.branch], cwd=wt.repo_dir, capture_output=True, text=True)


def list_worktrees(*, repo_dir: str = ".") -> list:
    """Parses `git worktree list --porcelain` into a list of
    {"path", "branch"} dicts -- used to confirm parallel worktrees
    coexist and that cleanup actually removed them."""
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise WorktreeError(f"git worktree list failed: {result.stderr.strip()}")
    entries = []
    current = {}
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if current:
                entries.append(current)
            current = {"path": line[len("worktree "):], "branch": None}
        elif line.startswith("branch "):
            current["branch"] = line[len("branch "):]
    if current:
        entries.append(current)
    return entries
