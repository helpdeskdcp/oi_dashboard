"""
agents/dev_agent/gates/code_quality.py -- Gate 5, added on top of the
original four per explicit request: linting, type checking, security
scanning, and dependency vulnerability scanning, so AI-generated patches
stay maintainable and secure, not just functionally passing.

Each check degrades gracefully (skipped, not failed) when its tool isn't
installed in the current environment -- the same is_configured()-style
pattern agents/llm_providers/* uses for missing SDKs -- so this gate
never spuriously blocks a proposal where the optional tooling hasn't
been installed. Installing ruff/mypy/bandit/pip-audit (see
requirements-dev.txt) switches it from advisory to fully enforcing with
no code change here.
"""
import shutil
import subprocess

from .base import GateResult, GateStatus

GATE_NAME = "code_quality"

# (check name, binary, args run from the worktree root)
CHECKS = (
    ("lint", "ruff", ["check", "."]),
    ("type_check", "mypy", ["--ignore-missing-imports", "."]),
    ("security_scan", "bandit", ["-q", "-r", "."]),
    ("dependency_scan", "pip-audit", []),
)


def _run_check(name, binary, args, cwd, timeout):
    if shutil.which(binary) is None:
        return {"name": name, "tool": binary, "status": "skipped", "reason": "tool not installed"}
    try:
        result = subprocess.run([binary, *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"name": name, "tool": binary, "status": "failed", "reason": "timed out"}
    ok = result.returncode == 0
    return {
        "name": name, "tool": binary, "status": "passed" if ok else "failed",
        "returncode": result.returncode, "output_tail": (result.stdout + result.stderr)[-3000:],
    }


def run(worktree_path: str, *, timeout: int = 180) -> GateResult:
    checks = [_run_check(name, binary, args, worktree_path, timeout) for name, binary, args in CHECKS]
    failed = [c for c in checks if c["status"] == "failed"]
    skipped = [c for c in checks if c["status"] == "skipped"]
    ran = [c for c in checks if c["status"] != "skipped"]

    if failed:
        status = GateStatus.FAILED
        summary = f"{len(failed)}/{len(checks)} quality checks failed: {', '.join(c['name'] for c in failed)}"
    elif not ran:
        status = GateStatus.SKIPPED
        summary = "all quality tools unavailable in this environment -- gate is advisory-only"
    else:
        note = f" ({len(skipped)} skipped: tool not installed)" if skipped else ""
        status = GateStatus.PASSED
        summary = f"{len(ran)}/{len(checks)} quality checks passed{note}"

    return GateResult(gate=GATE_NAME, status=status, summary=summary, details={"checks": checks})
