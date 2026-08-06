"""
test_agents/dev_agent/test_worktree.py -- regression tests for
agents/dev_agent/worktree.py, against a real throwaway git repo (never
this project's own repo).
"""
import os

import pytest

from agents.dev_agent import worktree
from .conftest import commit_on_new_branch, git


class TestCreate:
    def test_creates_a_new_branch_and_directory(self, toy_repo):
        wt = worktree.create("add-feature", repo_dir=str(toy_repo))
        try:
            assert os.path.isdir(wt.path) or os.path.isdir(str(toy_repo / wt.path))
            branches = git(toy_repo, "branch", "--list", wt.branch)
            assert wt.branch in branches
        finally:
            worktree.remove(wt)

    def test_never_touches_base_ref(self, toy_repo):
        before = git(toy_repo, "rev-parse", "main")
        wt = worktree.create("some-change", repo_dir=str(toy_repo), base_ref="main")
        try:
            (toy_repo / wt.path / "new_file.txt").write_text("candidate-only change\n")
            git(toy_repo / wt.path, "add", "new_file.txt")
            git(toy_repo / wt.path, "commit", "-q", "-m", "candidate change")
            after = git(toy_repo, "rev-parse", "main")
            assert before == after
            assert not (toy_repo / "new_file.txt").exists()
        finally:
            worktree.remove(wt)

    def test_branch_name_is_namespaced_under_agent_dev(self, toy_repo):
        wt = worktree.create("Fix The Bug!!", repo_dir=str(toy_repo))
        try:
            assert wt.branch.startswith("agent/dev-")
            assert "fix-the-bug" in wt.branch
        finally:
            worktree.remove(wt)

    def test_parallel_worktrees_coexist(self, toy_repo):
        wt_a = worktree.create("change-a", repo_dir=str(toy_repo))
        wt_b = worktree.create("change-b", repo_dir=str(toy_repo))
        try:
            assert wt_a.path != wt_b.path
            assert wt_a.branch != wt_b.branch
            listed_paths = {os.path.basename(e["path"]) for e in worktree.list_worktrees(repo_dir=str(toy_repo))}
            assert os.path.basename(wt_a.path) in listed_paths
            assert os.path.basename(wt_b.path) in listed_paths
        finally:
            worktree.remove(wt_a)
            worktree.remove(wt_b)


class TestCheckoutExisting:
    def test_attaches_to_an_existing_branch_without_creating_one(self, toy_repo):
        commit_on_new_branch(toy_repo, "agent/dev-candidate-1", "candidate.txt", "candidate content\n")
        wt = worktree.checkout_existing("agent/dev-candidate-1", repo_dir=str(toy_repo))
        try:
            assert (toy_repo / wt.path / "candidate.txt").read_text() == "candidate content\n"
        finally:
            worktree.remove(wt)

    def test_unknown_branch_raises(self, toy_repo):
        with pytest.raises(worktree.WorktreeError):
            worktree.checkout_existing("does-not-exist", repo_dir=str(toy_repo))


class TestRemove:
    def test_removes_directory_and_branch(self, toy_repo):
        wt = worktree.create("throwaway", repo_dir=str(toy_repo))
        worktree.remove(wt)
        assert not (toy_repo / wt.path).exists()
        branches = git(toy_repo, "branch", "--list", wt.branch)
        assert wt.branch not in branches

    def test_is_idempotent(self, toy_repo):
        wt = worktree.create("throwaway-2", repo_dir=str(toy_repo))
        worktree.remove(wt)
        worktree.remove(wt)  # must not raise on an already-gone worktree


class TestListWorktrees:
    def test_reflects_current_state(self, toy_repo):
        before = worktree.list_worktrees(repo_dir=str(toy_repo))
        wt = worktree.create("visible-change", repo_dir=str(toy_repo))
        try:
            during = worktree.list_worktrees(repo_dir=str(toy_repo))
            assert len(during) == len(before) + 1
        finally:
            worktree.remove(wt)
        after = worktree.list_worktrees(repo_dir=str(toy_repo))
        assert len(after) == len(before)
