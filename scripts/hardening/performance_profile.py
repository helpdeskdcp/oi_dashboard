"""
scripts/hardening/performance_profile.py -- Production Hardening
Sprint: "Performance profiling."

Real wall-clock timing (time.perf_counter, N repetitions, min/median/
max reported -- not a single noisy sample) of the hot paths that don't
need a live oi_history.db or a live broker session to run for real in
this environment: pure risk math, the sysadmin infrastructure snapshot,
report construction/serialization, and maintenance's own repo-wide
static-analysis sweeps run against this actual repository.

Usage: python3 scripts/hardening/performance_profile.py
Writes hardening_results/performance_profile.json.
"""
import datetime as dt
import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from agents import audit_log
from agents.risk_manager import risk_engine
from agents.sys_admin import (
    infra_monitor,
    maintenance,
    security_audit,
    sysadmin_report,
    sysadmin_store,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _time_it(fn, *, repeats: int) -> dict:
    samples = []
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000)
    return {
        "repeats": repeats,
        "min_ms": round(min(samples), 3),
        "median_ms": round(statistics.median(samples), 3),
        "max_ms": round(max(samples), 3),
    }


def _agents_py_files():
    import subprocess
    result = subprocess.run(
        ["git", "-C", REPO_ROOT, "ls-files", "agents/*.py", "agents/**/*.py"],
        capture_output=True, text=True, check=True,
    )
    return [f"{REPO_ROOT}/{line}" for line in result.stdout.splitlines() if line]


def profile_risk_engine():
    points = [round(50 * ((-1) ** i) * (1 + i % 5), 1) for i in range(500)]
    checks = [
        risk_engine.position_sizing_check(120, capital=500000, risk_pct=1.0),
        risk_engine.capital_allocation_check(1.0, 3.5, limit_pct=5.0),
        risk_engine.exposure_check("NIFTY", 1.0, 2.0, limit_pct=4.0, group_label="symbol:NIFTY"),
    ]
    stress = risk_engine.stress_test(points, (-0.3, -0.5, -0.7))
    worst_stress_pct = max(abs(v["max_drawdown"]) for v in stress.values()) / 500000 * 100 if stress else 0.0

    def _score():
        var = risk_engine.value_at_risk(points, 0.95)
        cvar = risk_engine.expected_shortfall(points, 0.95)
        drawdown_sim = risk_engine.simulate_drawdown_distribution(points, trials=200, percentile=95)
        risk_engine.compute_risk_score(
            checks, var_pct_of_capital=abs(var) / 500000 * 100, cvar_pct_of_capital=abs(cvar) / 500000 * 100,
            drawdown_sim_pct_of_capital=abs(drawdown_sim["percentile"]) / 500000 * 100,
            worst_stress_pct_of_capital=worst_stress_pct,
            correlation_flags=0,
        )

    return _time_it(_score, repeats=200)


def profile_drawdown_simulation():
    points = [round(50 * ((-1) ** i) * (1 + i % 5), 1) for i in range(200)]
    return _time_it(
        lambda: risk_engine.simulate_drawdown_distribution(points, trials=500, percentile=95),
        repeats=20,
    )


def profile_infra_snapshot(db_path: str):
    return _time_it(lambda: infra_monitor.snapshot(db_path=db_path, check_network=False), repeats=30)


def profile_report_build_and_serialize():
    def _one():
        report = sysadmin_report.build(
            module="perf_test", action="probe", reason="r", confidence=50, evidence={"x": list(range(200))},
        )
        report.to_json()
    return _time_it(_one, repeats=500)


def profile_secret_scan_of_real_repo():
    files = _agents_py_files()
    return {**_time_it(lambda: security_audit.scan_for_secrets(files), repeats=10), "files_scanned": len(files)}


def profile_duplicate_block_detection_of_real_repo():
    files = _agents_py_files()
    return {**_time_it(lambda: maintenance.find_duplicate_blocks(files), repeats=5), "files_scanned": len(files)}


def main():
    tmp_db = "/tmp/hardening_perf_infra.db"
    original_audit_db_path = audit_log.DB_PATH
    original_sysadmin_db_path = sysadmin_store.DB_PATH
    audit_log.DB_PATH = tmp_db
    sysadmin_store.DB_PATH = tmp_db
    audit_log.init_db()  # infra_monitor.snapshot()'s queue_length() reads agent_audit_log
    sysadmin_store.init_db()  # snapshot() also persists its own findings (sysadmin_log)

    results = {
        "risk_engine.compute_risk_score (VaR/CVaR/drawdown/score, 500-point series)": profile_risk_engine(),
        "risk_engine.simulate_drawdown_distribution (500 bootstrap trials, 200-point series)": profile_drawdown_simulation(),
        "infra_monitor.snapshot (network checks disabled)": profile_infra_snapshot(tmp_db),
        "sysadmin_report.build + to_json": profile_report_build_and_serialize(),
        "security_audit.scan_for_secrets (real agents/*.py files)": profile_secret_scan_of_real_repo(),
        "maintenance.find_duplicate_blocks (real agents/*.py files)": profile_duplicate_block_detection_of_real_repo(),
    }
    audit_log.DB_PATH = original_audit_db_path
    sysadmin_store.DB_PATH = original_sysadmin_db_path
    os.remove(tmp_db)

    out = {"generated_at": dt.datetime.now().isoformat(), "results": results}
    out_dir = os.path.join(REPO_ROOT, "hardening_results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "performance_profile.json")
    with open(out_path, "w") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(json.dumps(out, indent=2, default=str))
    print(f"\nWritten to {out_path}")


if __name__ == "__main__":
    main()
