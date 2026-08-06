"""
test_agents/quant_researcher/test_promotion.py -- regression tests for
promotion.decide_promotion()'s objective candidate-vs-baseline rule.
"""
from agents.quant_researcher.promotion import decide_promotion


BASELINE = {
    "net_pnl": 1000.0, "profit_factor": 1.5, "sharpe_ratio": 0.8,
    "expectancy": 10.0, "recovery_factor": 2.0, "max_drawdown": 500.0,
}


def test_never_promotes_without_statistical_validation():
    better_everywhere = {**BASELINE, "net_pnl": 2000.0}
    decision = decide_promotion(better_everywhere, BASELINE, validation_passed=False)
    assert decision.should_promote is False
    assert "statistical validation" in decision.reasoning


def test_promotes_when_strictly_better_on_one_metric_and_worse_on_none():
    candidate = {**BASELINE, "net_pnl": 1500.0}
    decision = decide_promotion(candidate, BASELINE, validation_passed=True)
    assert decision.should_promote is True
    assert decision.comparison["net_pnl"]["delta"] == 500.0


def test_does_not_promote_when_worse_on_any_metric_even_if_better_on_others():
    candidate = {**BASELINE, "net_pnl": 5000.0, "max_drawdown": 900.0}  # much better P&L, worse drawdown
    decision = decide_promotion(candidate, BASELINE, validation_passed=True)
    assert decision.should_promote is False


def test_does_not_promote_an_identical_candidate():
    decision = decide_promotion(dict(BASELINE), BASELINE, validation_passed=True)
    assert decision.should_promote is False


def test_lower_drawdown_counts_as_better():
    candidate = {**BASELINE, "max_drawdown": 300.0}
    decision = decide_promotion(candidate, BASELINE, validation_passed=True)
    assert decision.should_promote is True
