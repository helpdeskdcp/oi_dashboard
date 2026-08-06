"""
agents/dev_agent/gates/backtest_compare.py -- Gate 3. Runs the same fixed
scenario (agents.config.BENCHMARK_BASELINE by default) against baseline
and candidate code as two separate subprocesses -- never two imports of
the same "backtest" module name in one process, which would collide via
sys.modules caching -- and hands both result sets to
regression_analyzer.compare().

Only runs when the diff touches a Priority-1 (strategy-relevant) file.
config.DETECTION_PRIORITY[1] is the single source of truth for that
list, shared with the detector (Milestone 3) so the two never drift
apart. Skipped (non-blocking) otherwise, per the plan: a diff that never
touches the trading logic has nothing for a backtest to regress.
"""
import json
import subprocess

from .base import GateResult, GateStatus
from .. import regression_analyzer
from ... import config

GATE_NAME = "backtest_compare"

_STRATEGY_FILES = tuple(config.DETECTION_PRIORITY[1])

_SCENARIO_SCRIPT = (
    "import json, sys\n"
    "import backtest\n"
    "symbol, date_from, date_to = sys.argv[1], sys.argv[2], sys.argv[3]\n"
    "trades, cycle_count, meta = backtest.simulate_dynamic_sr_v4_trades(symbol, date_from, date_to)\n"
    "stats = backtest.compute_advanced_trade_stats(trades)\n"
    "points = [t.get('points') or 0.0 for t in trades]\n"
    "print(json.dumps({'stats': stats, 'points': points, 'cycle_count': cycle_count}))\n"
)


def touches_strategy_file(changed_files) -> bool:
    return any(f in _STRATEGY_FILES or f.startswith(_STRATEGY_FILES) for f in changed_files)


def _run_scenario(repo_path, symbol, date_from, date_to, timeout):
    result = subprocess.run(
        ["python3", "-c", _SCENARIO_SCRIPT, symbol, date_from, date_to],
        cwd=repo_path, capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"backtest scenario failed in {repo_path}: {result.stderr[-2000:]}")
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    return regression_analyzer.enrich_stats(payload["stats"], payload["points"])


def run(baseline_path: str, candidate_path: str, changed_files: list, *,
        symbol=None, date_from=None, date_to=None, timeout: int = 300) -> GateResult:
    if not touches_strategy_file(changed_files):
        return GateResult(
            gate=GATE_NAME, status=GateStatus.SKIPPED,
            summary="no Priority-1 strategy file touched -- backtest comparison not required",
            details={"changed_files": changed_files},
        )

    baseline_cfg = config.BENCHMARK_BASELINE
    symbol = symbol or baseline_cfg["symbol"]
    date_from = date_from or baseline_cfg["date_from"]
    date_to = date_to or baseline_cfg["date_to"]

    try:
        baseline_stats = _run_scenario(baseline_path, symbol, date_from, date_to, timeout)
        candidate_stats = _run_scenario(candidate_path, symbol, date_from, date_to, timeout)
    except (subprocess.TimeoutExpired, RuntimeError, ValueError, KeyError) as exc:
        return GateResult(
            gate=GATE_NAME, status=GateStatus.FAILED,
            summary=f"backtest scenario could not be run: {exc}",
            details={"symbol": symbol, "date_from": date_from, "date_to": date_to},
        )

    comparison = regression_analyzer.compare(baseline_stats, candidate_stats)
    status = GateStatus.FAILED if comparison["regressions"] else GateStatus.PASSED
    summary = (
        f"{len(comparison['regressions'])} metric regression(s) vs baseline"
        if comparison["regressions"] else "no regression vs baseline on this scenario"
    )
    return GateResult(
        gate=GATE_NAME, status=status, summary=summary,
        details={
            "symbol": symbol, "date_from": date_from, "date_to": date_to,
            "baseline": baseline_stats, "candidate": candidate_stats, "comparison": comparison,
        },
    )
