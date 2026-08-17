"""
test_agents/trading_intelligence/test_institutional_flow_backtest.py --
regression tests for institutional_flow_backtest.py, the historical-
cycles/strikes-archive backtest for institutional_intelligence.
institutional_flow_findings(). Pure unit tests -- synthetic cycles/rows,
no real archive I/O (backtest_symbol()/backtest_all_watched_symbols()
themselves are exercised separately, live, against the real oi_history.db
archive -- see this milestone's own manual verification, mirroring
test_structure_backtest.py's own established convention for its sibling
backtest module).
"""
from unittest import mock

from oi_engine import StrikeRow

from agents.trading_intelligence import data_access
from agents.trading_intelligence import institutional_flow_backtest as ifb


def _cycle(date, hh, mm, ss, ltp, rows=None):
    time_str = f"{hh:02d}:{mm:02d}:{ss:02d}"
    return {
        "cycle": {"date": date, "ts": f"{date}T{time_str}", "time": time_str, "underlying_ltp": ltp},
        "rows": rows or [],
    }


def _row(strike=100, **kw):
    defaults = dict(ce_oi_chg=100, ce_vol=100, ce_signal="Neutral",
                     pe_oi_chg=100, pe_vol=100, pe_signal="Neutral")
    defaults.update(kw)
    return StrikeRow(strike=strike, **defaults)


def _baseline_day(date="2026-08-01", *, n=25, strike=100, base_ltp=100.0):
    """n quiet cycles, one minute apart starting 09:15, alternating the
    underlying by +-0.5 (a small, real, non-zero volatility reading) and a
    flat, never-triggering strike row -- exactly enough real history
    (n >= MIN_VOLATILITY_HISTORY_CYCLES + 1) for both the volatility
    threshold and institutional_flow_findings()'s own history[1:] check."""
    cycles = []
    for i in range(n):
        ltp = base_ltp + (0.5 if i % 2 else 0.0)
        cycles.append(_cycle(date, 9, 15 + i, 0, ltp, rows=[_row(strike=strike)]))
    return cycles


class TestPredictedDirection:
    def test_ce_long_buildup_is_bullish(self):
        assert ifb._predicted_direction("CE", "Long Buildup") == "BULLISH"

    def test_ce_short_buildup_is_bearish(self):
        assert ifb._predicted_direction("CE", "Short Buildup") == "BEARISH"

    def test_pe_long_buildup_is_bearish(self):
        assert ifb._predicted_direction("PE", "Long Buildup") == "BEARISH"

    def test_pe_short_buildup_is_bullish(self):
        assert ifb._predicted_direction("PE", "Short Buildup") == "BULLISH"


class TestWinLossThreshold:
    def test_none_below_minimum_history(self):
        cycles = _baseline_day(n=15)   # fewer than MIN_VOLATILITY_HISTORY_CYCLES+1=21 prior cycles
        assert ifb._win_loss_threshold(cycles, 14) is None

    def test_real_positive_threshold_with_enough_history(self):
        cycles = _baseline_day(n=25)
        result = ifb._win_loss_threshold(cycles, 24)
        assert result is not None
        assert result > 0

    def test_never_crosses_day_boundary(self):
        day1 = _baseline_day(date="2026-08-01", n=25)
        day2 = [_cycle("2026-08-02", 9, 15, 0, 200.0, rows=[_row()])]
        cycles = day1 + day2
        # day2's own single cycle has zero SAME-DAY prior readings.
        assert ifb._win_loss_threshold(cycles, len(day1)) is None


class TestWalkForwardOutcome:
    def test_bullish_win(self):
        cycles = [
            _cycle("2026-08-01", 9, 15, 0, 100.0),
            _cycle("2026-08-01", 9, 16, 0, 106.0),   # +6 move
        ]
        assert ifb._walk_forward_outcome(cycles, start_idx=0, direction="BULLISH", threshold=3) == "WIN"

    def test_bullish_loss(self):
        cycles = [
            _cycle("2026-08-01", 9, 15, 0, 100.0),
            _cycle("2026-08-01", 9, 16, 0, 94.0),   # -6 move
        ]
        assert ifb._walk_forward_outcome(cycles, start_idx=0, direction="BULLISH", threshold=3) == "LOSS"

    def test_bearish_win(self):
        cycles = [
            _cycle("2026-08-01", 9, 15, 0, 100.0),
            _cycle("2026-08-01", 9, 16, 0, 94.0),
        ]
        assert ifb._walk_forward_outcome(cycles, start_idx=0, direction="BEARISH", threshold=3) == "WIN"

    def test_bearish_loss(self):
        cycles = [
            _cycle("2026-08-01", 9, 15, 0, 100.0),
            _cycle("2026-08-01", 9, 16, 0, 106.0),
        ]
        assert ifb._walk_forward_outcome(cycles, start_idx=0, direction="BEARISH", threshold=3) == "LOSS"

    def test_pending_when_nothing_resolves(self):
        cycles = [
            _cycle("2026-08-01", 9, 15, 0, 100.0),
            _cycle("2026-08-01", 9, 16, 0, 100.2),
        ]
        assert ifb._walk_forward_outcome(cycles, start_idx=0, direction="BULLISH", threshold=10) == "PENDING"

    def test_never_crosses_day_boundary(self):
        cycles = [
            _cycle("2026-08-01", 9, 15, 0, 100.0),
            _cycle("2026-08-02", 9, 15, 0, 500.0),   # huge move, but the next day
        ]
        assert ifb._walk_forward_outcome(cycles, start_idx=0, direction="BULLISH", threshold=5) == "PENDING"

    def test_stops_after_lookforward_window_elapses(self):
        cycles = [
            _cycle("2026-08-01", 9, 15, 0, 100.0),
            _cycle("2026-08-01", 9, 15 + ifb.OUTCOME_LOOKFORWARD_MINUTES + 10, 0, 500.0),
        ]
        assert ifb._walk_forward_outcome(cycles, start_idx=0, direction="BULLISH", threshold=5) == "PENDING"


class TestNoLookaheadLeakage:
    def test_never_calls_the_unbounded_live_lookup(self):
        """institutional_flow_findings() internally calls data_access.
        recent_strike_history() -- if backtest_symbol() ever let that reach
        the real (unpatched) function during a historical replay, THIS
        poisoned stand-in would be invoked. It never should be: the whole
        per-symbol replay runs inside backtest_symbol()'s own bounded-
        history patch."""
        poisoned = mock.MagicMock(side_effect=lambda *a, **k: [])
        cycles = _baseline_day(n=25)
        # Cycle 25: a real buildup signal, with history[1:] (fed from the
        # 24 baseline cycles above) showing a clear OI/volume expansion --
        # exercises the actual institutional_flow_findings() call path.
        cycles.append(_cycle("2026-08-01", 9, 40, 0, 100.0, rows=[
            _row(ce_oi_chg=1000, ce_vol=1000, ce_signal="Long Buildup"),
        ]))
        with mock.patch.object(data_access, "recent_strike_history", poisoned):
            ifb.backtest_symbol("NIFTY", "2026-08-01", "2026-08-01", cycles=cycles)
        assert poisoned.call_count == 0

    def test_patch_is_fully_restored_after_the_call(self):
        original = data_access.recent_strike_history
        cycles = _baseline_day(n=25)
        ifb.backtest_symbol("NIFTY", "2026-08-01", "2026-08-01", cycles=cycles)
        assert data_access.recent_strike_history is original


class TestBacktestSymbol:
    def test_empty_cycles_reports_zero_samples(self):
        result = ifb.backtest_symbol("NIFTY", "2026-08-01", "2026-08-01", cycles=[])
        assert result.sample_size == 0
        assert result.win_rate is None

    def test_never_raises_on_a_short_series(self):
        cycles = [_cycle("2026-08-01", 9, 15, 0, 100.0, rows=[_row()])]
        result = ifb.backtest_symbol("NIFTY", "2026-08-01", "2026-08-01", cycles=cycles)
        assert result.sample_size == 0

    def test_win_rate_is_none_below_minimum_sample_size(self):
        # A quiet series with no real institutional-flow findings at all.
        cycles = _baseline_day(n=25)
        result = ifb.backtest_symbol("NIFTY", "2026-08-01", "2026-08-01", cycles=cycles)
        assert result.win_rate is None
        assert result.sample_size == 0

    def test_a_real_finding_that_resolves_win_is_counted(self):
        cycles = _baseline_day(n=25)
        cycles.append(_cycle("2026-08-01", 9, 40, 0, 100.0, rows=[
            _row(ce_oi_chg=1000, ce_vol=1000, ce_signal="Long Buildup"),
        ]))
        # A clear bullish move right after the finding's own cycle.
        cycles.append(_cycle("2026-08-01", 9, 41, 0, 110.0, rows=[_row()]))
        result = ifb.backtest_symbol("NIFTY", "2026-08-01", "2026-08-01", cycles=cycles)
        assert result.wins == 1
        assert result.losses == 0

    def test_dedup_cooldown_collapses_consecutive_refires_into_one_event(self):
        cycles = _baseline_day(n=25)
        # The SAME (strike, side) buildup signal re-fires on 3 consecutive
        # cycles -- institutional_flow_findings()'s own real behavior while
        # OI stays elevated. Without the cooldown this would count as 3
        # independent samples instead of 1.
        for i in range(3):
            cycles.append(_cycle("2026-08-01", 9, 40 + i, 0, 100.0, rows=[
                _row(ce_oi_chg=1000, ce_vol=1000, ce_signal="Long Buildup"),
            ]))
        cycles.append(_cycle("2026-08-01", 9, 45, 0, 110.0, rows=[_row()]))
        result = ifb.backtest_symbol("NIFTY", "2026-08-01", "2026-08-01", cycles=cycles)
        assert result.wins + result.losses + result.pending == 1

    def test_finding_before_enough_history_is_excluded_not_counted_or_dropped_silently(self):
        # Only a handful of prior same-day cycles -- well below
        # MIN_VOLATILITY_HISTORY_CYCLES+1, so the win/loss threshold can't
        # be honestly computed.
        cycles = [_cycle("2026-08-01", 9, 15 + i, 0, 100.0, rows=[_row()]) for i in range(5)]
        cycles.append(_cycle("2026-08-01", 9, 21, 0, 100.0, rows=[
            _row(ce_oi_chg=1000, ce_vol=1000, ce_signal="Long Buildup"),
        ]))
        result = ifb.backtest_symbol("NIFTY", "2026-08-01", "2026-08-01", cycles=cycles)
        assert result.excluded_insufficient_history == 1
        assert result.sample_size == 0


class TestBacktestAllWatchedSymbols:
    def test_returns_one_result_per_watched_symbol(self):
        with mock.patch("agents.trading_intelligence.institutional_flow_backtest.backtest_symbol") as mocked:
            mocked.return_value = ifb.InstitutionalFlowBacktestResult(
                symbol="X", date_from="2026-08-01", date_to="2026-08-01",
                sample_size=0, wins=0, losses=0, pending=0,
                excluded_insufficient_history=0, win_rate=None,
            )
            from agents import config
            results = ifb.backtest_all_watched_symbols("2026-08-01", "2026-08-01")
        assert set(results.keys()) == set(config.TI_WATCHED_SYMBOLS)


class TestNoWritesToDatabase:
    def test_the_module_contains_no_sql_write_statements(self):
        """A concrete, automated proof of the read-only constraint -- not
        just a docstring claim -- mirroring test_safety.py's own AST-based
        posture for this package."""
        import ast
        import os

        path = os.path.join(
            os.path.dirname(__file__), "..", "..",
            "agents", "trading_intelligence", "institutional_flow_backtest.py",
        )
        with open(path, "r", errors="replace") as fh:
            source = fh.read()
        forbidden = ("INSERT", "UPDATE", "DELETE", "CREATE", ".commit(")
        found = [needle for needle in forbidden if needle in source]
        assert found == [], f"institutional_flow_backtest.py contains a write-shaped statement: {found}"
        ast.parse(source)   # also confirms the file is syntactically valid Python
