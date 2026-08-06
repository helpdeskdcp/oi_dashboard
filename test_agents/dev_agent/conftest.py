"""
test_agents/dev_agent/conftest.py -- shared toy-repo fixture for
Milestone 2 tests. A real, throwaway git repo in tmp_path -- never this
project's own repo -- with an initial commit on "main", used by every
test that exercises git worktree add/remove or git diff.
"""
import subprocess

import pytest


def git(repo_dir, *args):
    result = subprocess.run(["git", *args], cwd=str(repo_dir), capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout.strip()


@pytest.fixture()
def toy_repo(tmp_path):
    repo_dir = tmp_path / "toy_repo"
    repo_dir.mkdir()
    git(repo_dir, "init", "-q", "-b", "main")
    git(repo_dir, "config", "user.email", "test@example.com")
    git(repo_dir, "config", "user.name", "Test")
    (repo_dir / "README.md").write_text("hello\n")
    (repo_dir / "agents").mkdir()
    (repo_dir / "agents" / "placeholder.py").write_text("# agent framework file\n")
    git(repo_dir, "add", ".")
    git(repo_dir, "commit", "-q", "-m", "initial")
    return repo_dir


def commit_on_new_branch(repo_dir, branch, filename, content, *, base="main"):
    """Creates `branch` off `base`, commits one file change on it, and
    switches back to `base` -- so the caller's later `git worktree add
    <path> <branch>` doesn't collide with a branch already checked out
    in the primary working directory."""
    git(repo_dir, "checkout", "-q", "-b", branch, base)
    path = repo_dir / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    git(repo_dir, "add", filename)
    git(repo_dir, "commit", "-q", "-m", f"change on {branch}")
    git(repo_dir, "checkout", "-q", base)
