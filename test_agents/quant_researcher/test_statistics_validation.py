"""
test_agents/quant_researcher/test_statistics_validation.py -- regression
tests for statistics_validation.validate()'s two independent gates:
minimum trade count, and statistical significance of mean per-trade P&L.
"""
from agents.quant_researcher.statistics_validation import validate


def _trades(points):
    return [{"points": p} for p in points]


class TestMinimumTradeCount:
    def test_fails_below_minimum_regardless_of_how_good_the_numbers_look(self):
        trades = _trades([100.0] * 5)  # tiny sample, huge mean
        result = validate(trades, min_trades=30)
        assert result.passed is False
        assert "5 trade" in result.reason


class TestSignificance:
    def test_passes_with_a_large_consistently_positive_sample(self):
        trades = _trades([10.0, 12.0, 8.0, 11.0, 9.0] * 10)  # 50 trades, consistently positive
        result = validate(trades, min_trades=30, confidence_level=0.95)
        assert result.passed is True
        assert result.mean_points > 0

    def test_fails_when_mean_is_negative(self):
        trades = _trades([-10.0, -8.0, -12.0, -9.0, -11.0] * 10)
        result = validate(trades, min_trades=30, confidence_level=0.95)
        assert result.passed is False

    def test_fails_when_noisy_around_zero_even_at_large_sample_size(self):
        trades = _trades(([50.0, -49.0] * 25))  # mean ~0.5, huge variance
        result = validate(trades, min_trades=30, confidence_level=0.99)
        assert result.passed is False

    def test_zero_variance_positive_mean_passes(self):
        trades = _trades([5.0] * 40)
        result = validate(trades, min_trades=30)
        assert result.passed is True
        assert result.p_value == 0.0
