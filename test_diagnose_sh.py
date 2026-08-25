"""
test_diagnose_sh.py -- smoke tests for diagnose.sh, the local-first,
read-only diagnostics entrypoint. Does not run the full script (too slow
for CI, and several of its checks -- pytest, pip-audit -- would
recursively invoke themselves); just verifies it's present, executable,
syntactically valid, and doesn't reference anything that would make it
write to source files, git history, or place a broker order.
"""
import os
import shutil
import subprocess

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(REPO_ROOT, "diagnose.sh")


class TestDiagnoseScriptExists:
    def test_file_exists_and_is_executable(self):
        assert os.path.isfile(SCRIPT)
        assert os.access(SCRIPT, os.X_OK)


class TestDiagnoseScriptSyntax:
    def test_bash_syntax_is_valid(self):
        result = subprocess.run(["bash", "-n", SCRIPT], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_shellcheck_passes_at_warning_level(self):
        if shutil.which("shellcheck") is None:
            return  # advisory tool, degrades gracefully like agents/dev_agent/gates/code_quality.py's own checks
        result = subprocess.run(
            ["shellcheck", "-S", "warning", SCRIPT], capture_output=True, text=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr


class TestDiagnoseScriptSafety:
    def test_never_places_a_broker_order_or_touches_live_positions_route(self):
        with open(SCRIPT) as fh:
            content = fh.read()
        for banned in ("placeOrder", "modifyOrder", "cancelOrder", "/live-positions"):
            assert banned not in content

    def test_never_auto_commits_or_pushes_or_force_operations(self):
        with open(SCRIPT) as fh:
            content = fh.read()
        for banned in ("git commit", "git push", "git reset --hard", "git clean", "rm -rf"):
            assert banned not in content

    def test_never_runs_vibe_diagnosis_or_reinstalls_it(self):
        # vibe-diagnosis is permanently out of scope for this project's
        # diagnostics per explicit user instruction -- this script must
        # remain fully independent of it.
        with open(SCRIPT) as fh:
            content = fh.read()
        assert "vibe-diagnosis" not in content.lower()

    def test_unknown_flag_exits_nonzero_rather_than_running_full_sweep(self):
        result = subprocess.run(
            [SCRIPT, "--not-a-real-flag"], capture_output=True, text=True, cwd=REPO_ROOT,
        )
        assert result.returncode == 2
