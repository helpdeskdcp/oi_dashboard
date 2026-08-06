"""
agents/dev_agent/gates/unit_tests.py -- Gate 1. Runs the full pytest
suite inside the candidate worktree, exactly as a human running `pytest`
from the repo root would. Structured pass/fail plus captured output
feeds straight into the audit trail.
"""
import subprocess
import time

from .base import GateResult, GateStatus

GATE_NAME = "unit_tests"


def run(worktree_path: str, *, timeout: int = 600) -> GateResult:
    start = time.monotonic()
    try:
        result = subprocess.run(
            ["python3", "-m", "pytest", "-q"],
            cwd=worktree_path, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return GateResult(
            gate=GATE_NAME, status=GateStatus.FAILED,
            summary=f"pytest timed out after {timeout}s",
            details={"timeout_seconds": timeout},
        )

    elapsed = round(time.monotonic() - start, 1)
    passed = result.returncode == 0
    return GateResult(
        gate=GATE_NAME,
        status=GateStatus.PASSED if passed else GateStatus.FAILED,
        summary=f"pytest exited {result.returncode} in {elapsed}s",
        details={
            "returncode": result.returncode, "elapsed_seconds": elapsed,
            "stdout_tail": result.stdout[-4000:], "stderr_tail": result.stderr[-4000:],
        },
    )
