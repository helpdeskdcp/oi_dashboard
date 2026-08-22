"""
agents/dev_agent/gates/integration_tests.py -- Gate 2. Runs an app-boot
smoke check (`SKIP_AUTOSTART=1 python3 -c "import app"`, matching this
project's own manual post-merge verification step) followed by the
Flask-test-client route test file. Both must pass; the boot check runs
first since a route-test failure is uninformative if the app doesn't
even import.
"""
import os
import subprocess
import sys
import time

from .base import GateResult, GateStatus

GATE_NAME = "integration_tests"
DEFAULT_TEST_PATH = "test_backtest_profiles.py"


def _boot_smoke_check(worktree_path, timeout):
    env = dict(os.environ, SKIP_AUTOSTART="1")
    return subprocess.run(
        # sys.executable, never a bare "python3" -- see unit_tests.py's own
        # note: the venv interpreter is the one with this app's dependencies.
        [sys.executable, "-c", "import app"],
        cwd=worktree_path, capture_output=True, text=True, timeout=timeout, env=env,
    )


def run(worktree_path: str, *, test_path: str = DEFAULT_TEST_PATH, timeout: int = 120) -> GateResult:
    start = time.monotonic()
    try:
        boot = _boot_smoke_check(worktree_path, timeout)
    except subprocess.TimeoutExpired:
        return GateResult(
            gate=GATE_NAME, status=GateStatus.FAILED,
            summary=f"app boot smoke check timed out after {timeout}s",
            details={"stage": "boot"},
        )
    if boot.returncode != 0:
        return GateResult(
            gate=GATE_NAME, status=GateStatus.FAILED,
            summary="app boot smoke check failed (import app)",
            details={"stage": "boot", "stderr_tail": boot.stderr[-4000:]},
        )

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", test_path],
            cwd=worktree_path, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return GateResult(
            gate=GATE_NAME, status=GateStatus.FAILED,
            summary=f"route test suite timed out after {timeout}s",
            details={"stage": "route_tests"},
        )

    elapsed = round(time.monotonic() - start, 1)
    passed = result.returncode == 0
    return GateResult(
        gate=GATE_NAME,
        status=GateStatus.PASSED if passed else GateStatus.FAILED,
        summary=f"boot smoke check OK; route tests exited {result.returncode} in {elapsed}s",
        details={
            "stage": "route_tests", "returncode": result.returncode, "elapsed_seconds": elapsed,
            "stdout_tail": result.stdout[-4000:], "stderr_tail": result.stderr[-4000:],
        },
    )
