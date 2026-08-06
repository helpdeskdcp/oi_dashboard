"""
test_agents/dev_agent/test_patch_generator.py -- regression tests for
agents/dev_agent/patch_generator.py against a real throwaway git repo.
"""
import pytest

from agents.dev_agent import patch_generator as pg
from .conftest import commit_on_new_branch


class TestGenerateAndChangedFiles:
    def test_generate_returns_the_actual_diff_content(self, toy_repo):
        commit_on_new_branch(toy_repo, "candidate-1", "feature.py", "VALUE = 1\n")
        diff = pg.generate(str(toy_repo), "main", "candidate-1")
        assert "feature.py" in diff
        assert "+VALUE = 1" in diff

    def test_changed_files_lists_exactly_the_touched_files(self, toy_repo):
        commit_on_new_branch(toy_repo, "candidate-2", "another_feature.py", "X = 2\n")
        files = pg.changed_files(str(toy_repo), "main", "candidate-2")
        assert files == ["another_feature.py"]

    def test_unrelated_files_are_not_reported(self, toy_repo):
        commit_on_new_branch(toy_repo, "candidate-3", "only_this.py", "Y = 3\n")
        files = pg.changed_files(str(toy_repo), "main", "candidate-3")
        assert "README.md" not in files

    def test_invalid_ref_raises(self, toy_repo):
        with pytest.raises(pg.PatchGenerationError):
            pg.changed_files(str(toy_repo), "main", "no-such-branch")


class TestTouchesGuardedPath:
    def test_true_when_a_file_is_under_the_guarded_prefix(self):
        assert pg.touches_guarded_path(["agents/base_agent.py"], "agents/")

    def test_true_for_the_prefix_directory_itself(self):
        assert pg.touches_guarded_path(["agents/"], "agents/")

    def test_false_when_no_file_matches(self):
        assert not pg.touches_guarded_path(["app.py", "backtest.py"], "agents/")

    def test_does_not_false_positive_on_similarly_named_paths(self):
        # "agents_docs.py" shares a prefix character-wise but is not
        # UNDER agents/ -- must not be guarded.
        assert not pg.touches_guarded_path(["agents_docs.py"], "agents/")
