"""
test_agents/trading_intelligence/test_structure_backtest.py -- Milestone
20, Phase 7: regression tests for structure_backtest.py, the historical-
candle-archive backtest for institutional_levels.detect_role_reversal()'s
tunable parameters. Pure unit tests -- synthetic candle series, no real
archive I/O (backtest_symbol() itself is exercised separately, live,
against the real archive -- see the manual verification in this
milestone's own deployment notes).
"""
import datetime as dt

from agents.trading_intelligence import structure_backtest as sb


def _candle(minute, o, h, l, c, v=1000):
    base = dt.datetime(2026, 1, 1, 9, 0)
    return {"datetime": base + dt.timedelta(minutes=minute), "open": o, "high": h, "low": l, "close": c, "volume": v}


class TestFindPivots:
    def test_finds_a_real_swing_high(self):
        candles = [_candle(i, 100, 100.1, 99.9, 100) for i in range(5)]
        candles.append(_candle(5, 100, 110, 99.9, 105))   # spike high at index 5
        candles += [_candle(i, 100, 100.1, 99.9, 100) for i in range(6, 11)]
        pivots = sb._find_pivots(candles, window=5)
        highs = [p for p in pivots if p["type"] == "RESISTANCE"]
        assert any(p["index"] == 5 and p["level"] == 110 for p in highs)

    def test_finds_a_real_swing_low(self):
        candles = [_candle(i, 100, 100.1, 99.9, 100) for i in range(5)]
        candles.append(_candle(5, 100, 100.1, 90, 95))   # spike low at index 5
        candles += [_candle(i, 100, 100.1, 99.9, 100) for i in range(6, 11)]
        pivots = sb._find_pivots(candles, window=5)
        lows = [p for p in pivots if p["type"] == "SUPPORT"]
        assert any(p["index"] == 5 and p["level"] == 90 for p in lows)

    def test_a_real_pivot_is_distinguishable_from_the_surrounding_flat_run(self):
        candles = [_candle(i, 100, 100, 100, 100) for i in range(5)]
        candles.append(_candle(5, 100, 110, 100, 105))
        candles += [_candle(i, 100, 100, 100, 100) for i in range(6, 11)]
        pivots = sb._find_pivots(candles, window=5)
        assert any(p["index"] == 5 and p["level"] == 110 for p in pivots)


class TestWalkForwardOutcome:
    def test_bullish_target_hit_first_is_a_win(self):
        candles = [_candle(0, 100, 101, 99, 100), _candle(1, 100, 120, 100, 115)]
        overlay = {"direction": "BULLISH", "entry": 100, "sl": 95, "t1": 110, "t2": 120}
        assert sb._walk_forward_outcome(candles, start_idx=0, overlay=overlay) == "WIN"

    def test_bullish_stop_hit_first_is_a_loss(self):
        candles = [_candle(0, 100, 101, 90, 95)]
        overlay = {"direction": "BULLISH", "entry": 100, "sl": 95, "t1": 110, "t2": 120}
        assert sb._walk_forward_outcome(candles, start_idx=0, overlay=overlay) == "LOSS"

    def test_bearish_target_hit_first_is_a_win(self):
        candles = [_candle(0, 100, 101, 85, 90)]
        overlay = {"direction": "BEARISH", "entry": 100, "sl": 105, "t1": 90, "t2": 80}
        assert sb._walk_forward_outcome(candles, start_idx=0, overlay=overlay) == "WIN"

    def test_neither_resolved_within_the_lookforward_window_is_pending(self):
        candles = [_candle(i, 100, 101, 99, 100) for i in range(5)]
        overlay = {"direction": "BULLISH", "entry": 100, "sl": 50, "t1": 200, "t2": 300}
        assert sb._walk_forward_outcome(candles, start_idx=0, overlay=overlay) == "PENDING"

    def test_same_candle_hitting_both_counts_as_a_loss_not_a_win(self):
        # Ties go to the stop -- never the more favorable reading.
        candles = [_candle(0, 100, 120, 90, 100)]
        overlay = {"direction": "BULLISH", "entry": 100, "sl": 95, "t1": 110, "t2": 120}
        assert sb._walk_forward_outcome(candles, start_idx=0, overlay=overlay) == "LOSS"


class TestBacktestParameters:
    def test_empty_candles_reports_zero_samples(self):
        result = sb.backtest_parameters("NIFTY", [], max_retest_candles=3, min_volume_multiplier=1.2)
        assert result.sample_size == 0
        assert result.win_rate is None

    def test_win_rate_is_none_below_minimum_sample_size(self):
        # A tiny, mostly-flat series -- at most a couple of real pivots,
        # nowhere near BACKTEST_MIN_SAMPLE_SIZE.
        candles = [_candle(i, 100, 100.1, 99.9, 100) for i in range(30)]
        result = sb.backtest_parameters("NIFTY", candles, max_retest_candles=3, min_volume_multiplier=1.2)
        assert result.win_rate is None

    def test_never_raises_on_a_short_series(self):
        candles = [_candle(0, 100, 101, 99, 100)]
        result = sb.backtest_parameters("NIFTY", candles, max_retest_candles=3, min_volume_multiplier=1.2)
        assert result.sample_size == 0


class TestBacktestSymbol:
    def test_returns_one_result_per_grid_combination(self):
        candles = [_candle(i, 100, 100.1, 99.9, 100) for i in range(50)]
        grid = [(2, 1.0), (3, 1.2), (4, 1.5)]
        results = sb.backtest_symbol("NIFTY", param_grid=grid, candles=candles)
        assert len(results) == 3
        assert {(r.max_retest_candles, r.min_volume_multiplier) for r in results} == set(grid)

    def test_results_are_sorted_best_win_rate_first(self):
        candles = [_candle(i, 100, 100.1, 99.9, 100) for i in range(50)]
        results = sb.backtest_symbol("NIFTY", param_grid=[(2, 1.0), (3, 1.2)], candles=candles)
        rates = [r.win_rate for r in results if r.win_rate is not None]
        assert rates == sorted(rates, reverse=True)

    def test_default_grid_is_used_when_none_given(self):
        candles = [_candle(i, 100, 100.1, 99.9, 100) for i in range(50)]
        results = sb.backtest_symbol("NIFTY", candles=candles)
        assert len(results) > 1
