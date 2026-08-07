"""
Fast regression counterpart to scripts/hardening/performance_profile.py
-- smoke-tests that every profiled hot path still runs, and stays under
a generous time bound, WITHOUT asserting exact timings (that would be
flaky on shared/CI hardware). See PRODUCTION_HARDENING_SPRINT.md for the
real, repeatable numbers scripts/hardening/performance_profile.py
produced on this run.
"""
import time

from agents.risk_manager import risk_engine
from agents.sys_admin import infra_monitor, maintenance, sysadmin_report


def _elapsed_ms(fn):
    start = time.perf_counter()
    fn()
    return (time.perf_counter() - start) * 1000


class TestHotPathsCompleteQuickly:
    def test_risk_score_computation_completes_in_bounded_time(self):
        points = [round(50 * ((-1) ** i) * (1 + i % 5), 1) for i in range(500)]
        checks = [
            risk_engine.position_sizing_check(120, capital=500000, risk_pct=1.0),
            risk_engine.capital_allocation_check(1.0, 3.5, limit_pct=5.0),
        ]
        var = risk_engine.value_at_risk(points, 0.95)
        cvar = risk_engine.expected_shortfall(points, 0.95)
        drawdown_sim = risk_engine.simulate_drawdown_distribution(points, trials=200, percentile=95)

        elapsed = _elapsed_ms(lambda: risk_engine.compute_risk_score(
            checks, var_pct_of_capital=abs(var) / 500000 * 100, cvar_pct_of_capital=abs(cvar) / 500000 * 100,
            drawdown_sim_pct_of_capital=abs(drawdown_sim["percentile"]) / 500000 * 100,
            worst_stress_pct_of_capital=5.0, correlation_flags=0,
        ))
        assert elapsed < 1000, f"compute_risk_score took {elapsed:.1f}ms -- investigate a real regression"

    def test_infra_snapshot_completes_in_bounded_time(self, agent_db):
        elapsed = _elapsed_ms(lambda: infra_monitor.snapshot(db_path=agent_db, check_network=False))
        assert elapsed < 2000, f"infra_monitor.snapshot took {elapsed:.1f}ms -- investigate a real regression"

    def test_report_build_and_serialize_completes_in_bounded_time(self):
        def _one():
            report = sysadmin_report.build(module="perf_smoke", action="probe", reason="r", confidence=50, evidence={})
            report.to_json()
        elapsed = _elapsed_ms(_one)
        assert elapsed < 100, f"report build+serialize took {elapsed:.1f}ms -- investigate a real regression"

    def test_duplicate_block_detection_completes_in_bounded_time(self):
        import os
        this_file = os.path.abspath(__file__)
        elapsed = _elapsed_ms(lambda: maintenance.find_duplicate_blocks([this_file]))
        assert elapsed < 5000, f"find_duplicate_blocks took {elapsed:.1f}ms -- investigate a real regression"
