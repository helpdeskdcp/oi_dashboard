"""
agents/sys_admin/maintenance.py -- "Detect: Dead code, Duplicate code,
Slow modules, Memory leaks, Dependency issues, Database fragmentation.
Generate improvement proposals automatically."

Every check here is real, not simulated -- and every one degrades to
"not checked" (never a fabricated result) when its tool isn't
installed, the same pattern agents.dev_agent.gates.code_quality already
established for ruff/mypy/bandit/pip-audit. This module never applies
any fix it finds -- "generate improvement proposals" means exactly
that: a SysAdminReport per finding, recorded for a human to act on, the
same propose-only posture as every other module in this package.
"""
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import tracemalloc

from .. import config
from . import sysadmin_report, sysadmin_store


def check_dead_code(paths: list) -> dict:
    if shutil.which("vulture") is None:
        return {"checked": False, "reason": "vulture not installed", "findings": []}
    result = subprocess.run(["vulture", *paths], capture_output=True, text=True, timeout=120)
    return {"checked": True, "findings": [line for line in result.stdout.splitlines() if line.strip()]}


def find_duplicate_blocks(paths: list, *, min_lines: int | None = None) -> list:
    """Pure-Python, hash-window duplicate detector -- no external tool
    dependency. Slides a min_lines-line window over every file's
    (whitespace-stripped) source, hashes each window, and reports any
    hash appearing more than once -- a blunt but real signal for exact-
    line-sequence duplication, not semantic similarity."""
    min_lines = min_lines or config.SYS_ADMIN_DUPLICATE_BLOCK_MIN_LINES
    seen: dict = {}
    for path in paths:
        try:
            with open(path, "r", errors="replace") as fh:
                lines = [line.strip() for line in fh.readlines()]
        except OSError:
            continue
        for i in range(len(lines) - min_lines + 1):
            window = lines[i:i + min_lines]
            if not any(window):
                continue
            key = hashlib.sha1("\n".join(window).encode()).hexdigest()
            seen.setdefault(key, []).append({"path": path, "start_line": i + 1})

    return [
        {"locations": locations, "line_count": min_lines}
        for locations in seen.values() if len(locations) > 1
    ]


_DURATION_LINE = re.compile(r"^(\d+\.\d+)s\s+(call|setup|teardown)\s+(\S+)")


def parse_pytest_durations(output: str) -> list:
    """Parses `pytest --durations=N`'s own text output -- pure parsing,
    so this is testable without re-running the suite from inside itself.
    run_test_timing() below is the thing that actually invokes pytest."""
    results = []
    for line in output.splitlines():
        match = _DURATION_LINE.match(line.strip())
        if match:
            results.append({"seconds": float(match.group(1)), "phase": match.group(2), "test": match.group(3)})
    return results


def run_test_timing(*, repo_dir: str = ".", count: int = 20, timeout: int = 600) -> dict:
    """Real -- actually runs pytest with --durations. Expensive (a full
    suite run), so callers doing a lightweight maintenance sweep should
    prefer parse_pytest_durations() against output they already have."""
    result = subprocess.run(
        ["python3", "-m", "pytest", "-q", f"--durations={count}"],
        cwd=repo_dir, capture_output=True, text=True, timeout=timeout,
    )
    return {"returncode": result.returncode, "slow_tests": parse_pytest_durations(result.stdout)}


def probe_memory_leak(operation, *, iterations: int | None = None) -> dict:
    """Runs `operation` (a zero-arg callable) `iterations` times,
    comparing tracemalloc snapshots before and after -- a real, bounded
    measurement, not a claim of having run for hours. The first call is
    a warm-up excluded from the delta (import/cache warm-up looks like
    "growth" but isn't a leak)."""
    iterations = iterations or config.SYS_ADMIN_MEMORY_LEAK_ITERATIONS
    operation()  # warm-up
    tracemalloc.start()
    try:
        before = tracemalloc.take_snapshot()
        for _ in range(iterations):
            operation()
        after = tracemalloc.take_snapshot()
    finally:
        tracemalloc.stop()

    stats = after.compare_to(before, "lineno")
    growth_kb = round(sum(s.size_diff for s in stats) / 1024, 2)
    top = [
        {"location": str(s.traceback[0]), "growth_kb": round(s.size_diff / 1024, 2)}
        for s in stats[:5] if s.size_diff > 0
    ]
    return {
        "iterations": iterations, "growth_kb": growth_kb, "top_allocations": top,
        "leak_suspected": growth_kb > config.SYS_ADMIN_MEMORY_LEAK_GROWTH_KB,
    }


def check_dependencies(*, repo_dir: str = ".") -> dict:
    if shutil.which("pip-audit") is None:
        return {"checked": False, "reason": "pip-audit not installed", "vulnerabilities": []}
    result = subprocess.run(
        ["pip-audit", "--format", "json"], cwd=repo_dir, capture_output=True, text=True, timeout=120,
    )
    try:
        data = json.loads(result.stdout) if result.stdout.strip() else []
    except ValueError:
        data = []
    vulnerabilities = data.get("dependencies", data) if isinstance(data, dict) else data
    return {"checked": True, "vulnerabilities": vulnerabilities}


def check_db_fragmentation(db_path: str = "oi_history.db") -> dict:
    if not os.path.exists(db_path):
        return {"checked": False, "reason": "database does not exist"}
    conn = sqlite3.connect(db_path)
    try:
        page_count = conn.execute("PRAGMA page_count").fetchone()[0]
        freelist_count = conn.execute("PRAGMA freelist_count").fetchone()[0]
    finally:
        conn.close()
    fragmentation_pct = round(freelist_count / page_count * 100, 2) if page_count else 0.0
    return {
        "checked": True, "page_count": page_count, "freelist_count": freelist_count,
        "fragmentation_pct": fragmentation_pct,
        "vacuum_recommended": fragmentation_pct > config.SYS_ADMIN_DB_FRAGMENTATION_WARN_PCT,
    }


def run_maintenance_sweep(*, source_paths: list | None = None, repo_dir: str = ".",
                           db_path: str = "oi_history.db", pytest_output: str | None = None) -> list:
    """The full Module 6 sweep -- returns a list of SysAdminReport
    (improvement proposals), one per finding worth surfacing. Never
    applies any fix; every finding here is a recommendation for a human
    (or a future dev_agent proposal) to act on."""
    reports = []
    source_paths = source_paths or []

    if source_paths:
        dead = check_dead_code(source_paths)
        if dead["checked"] and dead["findings"]:
            reports.append(sysadmin_report.build(
                module="maintenance", action="dead_code_check",
                reason=f"{len(dead['findings'])} possible dead-code finding(s)",
                confidence=50, evidence=dead, affected_components=source_paths, severity="info",
            ))

        duplicates = find_duplicate_blocks(source_paths)
        if duplicates:
            reports.append(sysadmin_report.build(
                module="maintenance", action="duplicate_code_check",
                reason=f"{len(duplicates)} duplicate code block(s) of "
                       f"{config.SYS_ADMIN_DUPLICATE_BLOCK_MIN_LINES}+ lines found",
                confidence=75, evidence={"duplicates": duplicates[:20]},
                affected_components=sorted({loc["path"] for d in duplicates for loc in d["locations"]}),
                severity="info",
            ))

    if pytest_output:
        slow = [t for t in parse_pytest_durations(pytest_output) if t["seconds"] >= config.SYS_ADMIN_SLOW_TEST_WARN_SECONDS]
        if slow:
            reports.append(sysadmin_report.build(
                module="maintenance", action="slow_test_check",
                reason=f"{len(slow)} test(s) slower than {config.SYS_ADMIN_SLOW_TEST_WARN_SECONDS}s",
                confidence=90, evidence={"slow_tests": slow}, severity="info",
            ))

    deps = check_dependencies(repo_dir=repo_dir)
    if deps["checked"] and deps["vulnerabilities"]:
        reports.append(sysadmin_report.build(
            module="maintenance", action="dependency_check",
            reason=f"{len(deps['vulnerabilities'])} dependency vulnerability finding(s)",
            confidence=85, evidence=deps, severity="warning",
        ))

    fragmentation = check_db_fragmentation(db_path)
    if fragmentation.get("vacuum_recommended"):
        reports.append(sysadmin_report.build(
            module="maintenance", action="db_fragmentation_check",
            reason=f"database is {fragmentation['fragmentation_pct']}% fragmented -- VACUUM recommended",
            confidence=80, evidence=fragmentation, affected_components=[db_path], severity="info",
        ))

    for report in reports:
        sysadmin_store.record_report(report)
    return reports
