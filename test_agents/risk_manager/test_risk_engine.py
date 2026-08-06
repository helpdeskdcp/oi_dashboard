"""
test_agents/risk_manager/test_risk_engine.py -- regression tests for the
Promotion Risk Gate's pure math (agents/risk_manager/risk_engine.py).
"""
import datetime as dt
import random

import pytest

from agents.risk_manager import risk_engine


def _trade(points, exit_time=None):
    return {"points": points, "exit_time": exit_time or dt.datetime(2026, 5, 4)}


class TestSectorFor:
    def test_known_symbol_maps_to_its_sector(self):
        assert risk_engine.sector_for("NIFTY") == "index"
        assert risk_engine.sector_for("CRUDEOIL") == "energy"

    def test_unknown_symbol_falls_back_to_itself(self):
        assert risk_engine.sector_for("SOMENEWSYMBOL") == "SOMENEWSYMBOL"


class TestPositionSizing:
    def test_reasonable_stop_sizes_to_at_least_one_unit(self):
        result = risk_engine.position_sizing_check(20.0, capital=1_000_000, risk_pct=1.0)
        assert result.passed is True
        assert result.value >= 1

    def test_absurdly_wide_stop_sizes_to_zero_and_fails(self):
        result = risk_engine.position_sizing_check(10_000_000.0, capital=1_000_000, risk_pct=1.0)
        assert result.passed is False
        assert result.value == 0.0


class TestCapitalAndExposureChecks:
    def test_capital_allocation_passes_under_limit(self):
        result = risk_engine.capital_allocation_check(1.0, 5.0, limit_pct=20.0)
        assert result.passed is True
        assert result.value == 6.0

    def test_capital_allocation_fails_over_limit(self):
        result = risk_engine.capital_allocation_check(1.0, 25.0, limit_pct=20.0)
        assert result.passed is False

    def test_exposure_check_fails_over_limit(self):
        result = risk_engine.exposure_check(
            "exposure_symbol", 5.0, 5.0, limit_pct=8.0, group_label="symbol NIFTY",
        )
        assert result.passed is False
        assert result.value == 10.0

    def test_concurrent_trades_check_counts_the_candidate(self):
        result = risk_engine.concurrent_trades_check(9, limit=10)
        assert result.passed is True
        assert result.value == 10.0
        result2 = risk_engine.concurrent_trades_check(10, limit=10)
        assert result2.passed is False


class TestCorrelation:
    def test_daily_pnl_series_aggregates_same_day_trades(self):
        trades = [_trade(10, dt.datetime(2026, 5, 4, 10)), _trade(5, dt.datetime(2026, 5, 4, 14))]
        series = risk_engine.daily_pnl_series(trades)
        assert series == {"2026-05-04": 15}

    def test_perfectly_correlated_series(self):
        a = {"d1": 1.0, "d2": 2.0, "d3": 3.0}
        b = {"d1": 2.0, "d2": 4.0, "d3": 6.0}
        assert risk_engine.pearson_correlation(a, b) == pytest.approx(1.0)

    def test_insufficient_overlap_returns_none(self):
        a = {"d1": 1.0}
        b = {"d2": 1.0}
        assert risk_engine.pearson_correlation(a, b) is None

    def test_correlation_analysis_maps_by_strategy_name(self):
        candidate = [_trade(10, dt.datetime(2026, 5, 4))]
        active = [{"strategy_name": "other", "trades": [_trade(10, dt.datetime(2026, 5, 4))]}]
        result = risk_engine.correlation_analysis(candidate, active)
        assert "other" in result


class TestVarAndCvar:
    def test_var_zero_when_no_losses(self):
        assert risk_engine.value_at_risk([10, 20, 30], 0.95) == 0.0

    def test_var_positive_with_losses(self):
        points = [-100] * 5 + [10] * 95  # 5% tail losses -- exactly the 95% VaR boundary
        var = risk_engine.value_at_risk(points, 0.95)
        assert var > 0

    def test_cvar_is_never_less_than_var(self):
        points = [-100, -50, -10] + [5] * 97
        var = risk_engine.value_at_risk(points, 0.95)
        cvar = risk_engine.expected_shortfall(points, 0.95)
        assert cvar >= var

    def test_empty_points_are_safe(self):
        assert risk_engine.value_at_risk([], 0.95) == 0.0
        assert risk_engine.expected_shortfall([], 0.95) == 0.0


class TestDrawdownSimulation:
    def test_empty_points_returns_zeroed_result(self):
        result = risk_engine.simulate_drawdown_distribution([], trials=100, percentile=95)
        assert result == {"mean": 0.0, "percentile": 0.0, "worst": 0.0, "trials": 0}

    def test_all_winners_never_draws_down(self):
        result = risk_engine.simulate_drawdown_distribution(
            [10.0] * 20, trials=200, percentile=95, rng=random.Random(42),
        )
        assert result["mean"] == 0.0
        assert result["worst"] == 0.0

    def test_mixed_results_produce_a_nonzero_distribution(self):
        points = [20.0, -15.0, 10.0, -10.0, 5.0] * 10
        result = risk_engine.simulate_drawdown_distribution(points, trials=300, percentile=95, rng=random.Random(1))
        assert result["percentile"] >= 0
        assert result["worst"] >= result["percentile"]
        assert result["trials"] == 300


class TestStressTest:
    def test_winners_are_never_shocked(self):
        result = risk_engine.stress_test([10.0, 20.0], (-0.5,))
        assert result["-0.5"]["net_pnl"] == 30.0

    def test_losses_get_worse_under_a_negative_shock(self):
        baseline_loss = -10.0
        result = risk_engine.stress_test([baseline_loss], (-0.5,))
        assert result["-0.5"]["net_pnl"] == pytest.approx(-15.0)  # -10 * 1.5

    def test_full_loss_doubling_shock(self):
        result = risk_engine.stress_test([-10.0], (-1.0,))
        assert result["-1.0"]["net_pnl"] == pytest.approx(-20.0)


class TestComputeRiskScoreAndDecide:
    def test_all_checks_pass_and_metrics_within_limits_scores_high(self):
        checks = [risk_engine.RiskCheckResult("x", True, 1.0, 2.0, "ok")]
        score = risk_engine.compute_risk_score(
            checks, var_pct_of_capital=0.1, cvar_pct_of_capital=0.1,
            drawdown_sim_pct_of_capital=0.1, worst_stress_pct_of_capital=0.1, correlation_flags=0,
        )
        assert score == 100

    def test_failed_hard_check_deducts_fifteen(self):
        checks = [risk_engine.RiskCheckResult("x", False, 1.0, 2.0, "bad")]
        score = risk_engine.compute_risk_score(
            checks, var_pct_of_capital=0.0, cvar_pct_of_capital=0.0,
            drawdown_sim_pct_of_capital=0.0, worst_stress_pct_of_capital=0.0, correlation_flags=0,
        )
        assert score == 85

    def test_score_never_goes_below_zero(self):
        checks = [risk_engine.RiskCheckResult(f"c{i}", False, 1.0, 2.0, "bad") for i in range(10)]
        score = risk_engine.compute_risk_score(
            checks, var_pct_of_capital=1000, cvar_pct_of_capital=1000,
            drawdown_sim_pct_of_capital=1000, worst_stress_pct_of_capital=1000, correlation_flags=20,
        )
        assert score == 0

    def test_decide_thresholds(self, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "RISK_SCORE_REJECT_BELOW", 40)
        monkeypatch.setattr(config, "RISK_SCORE_REVIEW_BELOW", 70)
        assert risk_engine.decide(39) == "REJECTED"
        assert risk_engine.decide(40) == "REQUIRES_REVIEW"
        assert risk_engine.decide(69) == "REQUIRES_REVIEW"
        assert risk_engine.decide(70) == "APPROVED"


class TestEvaluatePromotion:
    def test_clean_candidate_with_no_active_strategies_is_approved(self):
        trades = [_trade(10.0, dt.datetime(2026, 5, 4) + dt.timedelta(days=i)) for i in range(40)]
        assessment = risk_engine.evaluate_promotion(
            candidate_name="test_strategy", symbol="NIFTY", strategy_family="oi_delta_combo",
            stop_points=15.0, trades=trades, active_strategies=[], capital=1_000_000,
        )
        assert assessment.decision == "APPROVED"
        assert assessment.risk_score >= 70
        assert all(c.passed for c in assessment.checks)

    def test_overexposed_symbol_is_penalized(self):
        trades = [_trade(10.0, dt.datetime(2026, 5, 4) + dt.timedelta(days=i)) for i in range(40)]
        active = [
            {"strategy_name": f"s{i}", "symbol": "NIFTY", "risk_pct": 3.0, "trades": []}
            for i in range(3)
        ]
        assessment = risk_engine.evaluate_promotion(
            candidate_name="test_strategy", symbol="NIFTY", strategy_family="oi_delta_combo",
            stop_points=15.0, trades=trades, active_strategies=active, capital=1_000_000,
        )
        exposure_check = next(c for c in assessment.checks if c.name == "exposure_symbol")
        assert exposure_check.passed is False
        assert assessment.risk_score < 100

    def test_explanation_is_a_nonempty_human_readable_string(self):
        trades = [_trade(10.0, dt.datetime(2026, 5, 4))]
        assessment = risk_engine.evaluate_promotion(
            candidate_name="t", symbol="NIFTY", strategy_family="f", stop_points=10.0,
            trades=trades, active_strategies=[],
        )
        assert isinstance(assessment.explanation, str)
        assert "Risk score" in assessment.explanation
