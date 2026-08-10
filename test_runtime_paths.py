"""
test_runtime_paths.py -- regression tests for runtime_paths.py, the
canonical topology module built after a real deployment prompt assumed
the wrong database filename, log location, and a pkill pattern broad
enough to hit an unrelated service on this shared VPS.
"""
import os
import subprocess
import sys

import runtime_paths


class TestCanonicalConstants:
    def test_app_root_is_this_files_own_directory(self):
        assert runtime_paths.APP_ROOT == os.path.dirname(os.path.abspath(runtime_paths.__file__))

    def test_all_paths_are_absolute(self):
        for name in ("DATABASE_PATH", "LOG_DIR", "APP_LOG_FILE", "RUN_FOREVER_LOG_FILE",
                     "PID_FILE", "SUPERVISOR_SCRIPT", "BACKUP_DIR"):
            value = getattr(runtime_paths, name)
            assert os.path.isabs(value), f"{name}={value!r} is not absolute"

    def test_database_path_defaults_to_oi_history_db(self, tmp_path):
        """Not asserted against the already-imported runtime_paths
        module's own DATABASE_PATH constant -- THIS worktree has no
        .env of its own (worktrees never get one; only the real repo
        root does), so that constant is only correct once this module
        lives at the actual deployed location. Instead, copies the real
        module + a .env matching the real one's own DB_PATH=oi_history.db
        line into an isolated temp dir, and imports it fresh in a
        subprocess whose environment is ALSO deliberately contaminated
        with a wrong DB_PATH first (reproducing the real
        /root/ai_trading_real leak found on this VPS) -- proving
        override=True actually wins, not just that an empty .env is a
        no-op (a python-dotenv .env only overrides keys it explicitly
        defines; this is the scenario that actually matters)."""
        (tmp_path / ".env").write_text("DB_PATH=oi_history.db\n")
        module_source = open(runtime_paths.__file__).read()
        (tmp_path / "runtime_paths.py").write_text(module_source)
        script = f"import sys; sys.path.insert(0, {str(tmp_path)!r}); import runtime_paths; print(runtime_paths.DATABASE_PATH)"
        contaminated_env = {**os.environ, "DB_PATH": "/wrong/contaminated/path.db"}
        result = subprocess.run(
            [sys.executable, "-c", script], env=contaminated_env, capture_output=True, text=True, timeout=10,
        )
        assert result.stdout.strip().endswith("oi_history.db"), result.stderr

    def test_log_dir_is_app_root_not_a_logs_subdirectory(self):
        """Real incident: this shared VPS has a logs/ directory, but it
        belongs to a different application entirely (different Linux
        user, unrelated date-partitioned structure)."""
        assert runtime_paths.LOG_DIR == runtime_paths.APP_ROOT

    def test_pid_file_matches_run_forever_vps_shs_own_write_target(self):
        assert runtime_paths.PID_FILE == os.path.join(runtime_paths.APP_ROOT, "run_forever.pid")


class TestEnvironmentContaminationResistance:
    """Real incident, found while building this module: this shared VPS
    has DB_PATH already exported in the shell environment by an
    unrelated project (/root/ai_trading_real). A script that reads
    os.getenv("DB_PATH", ...) without first loading THIS project's own
    .env (explicitly, by absolute path -- not python-dotenv's default
    upward-searching find_dotenv(), which has its own separate,
    documented fragility) would silently resolve to the wrong database.
    These tests verify the override MECHANISM runtime_paths.py relies
    on, in a clean subprocess -- module-level import-time constants
    can't be meaningfully re-tested via monkeypatch + re-import in the
    same process (Python caches the module)."""

    def test_explicit_dotenv_load_overrides_a_contaminated_shell_var(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("DB_PATH=correct_from_dotenv.db\n")
        script = (
            "import os\n"
            "from dotenv import load_dotenv\n"
            f"load_dotenv({str(env_file)!r}, override=True)\n"
            "print(os.getenv('DB_PATH'))\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            env={**os.environ, "DB_PATH": "/wrong/contaminated/path.db"},
            capture_output=True, text=True, timeout=10,
        )
        assert result.stdout.strip() == "correct_from_dotenv.db"

    def test_runtime_paths_module_itself_uses_absolute_path_not_find_dotenv(self):
        """Structural check: runtime_paths.py must load its own .env by
        an absolute, explicit path (os.path.join(APP_ROOT, ".env")),
        never a bare load_dotenv() call that relies on find_dotenv()'s
        upward directory search -- that search is exactly the mechanism
        that leaked the live production .env into worktree test runs
        during the Milestone 14 investigation (see docs/
        PRODUCTION_RUNTIME.md)."""
        source = open(runtime_paths.__file__).read()
        assert "load_dotenv(os.path.join(APP_ROOT" in source
        assert "override=True" in source
