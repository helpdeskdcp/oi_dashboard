"""Unit tests for agents/trading_intelligence/dual_probability_labels.py --
pure synthetic-data tests, no DB I/O, mirroring test_structure_backtest.py's
class-per-concern grouping."""
import datetime as dt

import pandas as pd
import pytest

from agents.trading_intelligence.dual_probability_labels import label_entry


def _candles(bars):
    """bars: list of (open, high, low, close). Builds a minimal
    datetime-sorted OHLC DataFrame, one bar per minute."""
    start = dt.datetime(2026, 1, 1, 9, 15)
    rows = []
    for i, (o, h, l, c) in enumerate(bars):
        rows.append({
            "datetime": start + dt.timedelta(minutes=i),
            "open": o, "high": h, "low": l, "close": c,
        })
    return pd.DataFrame(rows)


class TestBasicLabeling:
    def test_long_target_hit_before_stop(self):
        # entry at bar 0's open=100; target=110, stop=95
        candles = _candles([
            (100, 101, 99, 100),   # entry bar
            (100, 105, 99, 104),
            (104, 111, 103, 110),  # target touched here
        ])
        label = label_entry(candles, 0, direction="long", target_distance=10, stop_distance=5, horizon_bars=2)
        assert label.target_event is True
        assert label.stop_safety_event is True
        assert not label.truncated

    def test_long_stop_hit_before_target(self):
        candles = _candles([
            (100, 101, 99, 100),
            (100, 102, 94, 95),   # stop (95) touched here, target (110) not
            (95, 100, 90, 92),
        ])
        label = label_entry(candles, 0, direction="long", target_distance=10, stop_distance=5, horizon_bars=2)
        assert label.target_event is False
        assert label.stop_safety_event is False

    def test_same_bar_tie_resolves_to_stop(self):
        candles = _candles([
            (100, 101, 99, 100),
            (100, 111, 94, 105),  # both target(110) and stop(95) touched in ONE bar
        ])
        label = label_entry(candles, 0, direction="long", target_distance=10, stop_distance=5, horizon_bars=1)
        assert label.target_event is False  # tie -> adverse side wins

    def test_pending_when_neither_touched_within_horizon(self):
        candles = _candles([
            (100, 101, 99, 100),
            (100, 103, 98, 101),
            (101, 104, 99, 102),
        ])
        label = label_entry(candles, 0, direction="long", target_distance=10, stop_distance=5, horizon_bars=2)
        assert label.target_event is None

    def test_short_direction_mirrors_long(self):
        candles = _candles([
            (100, 101, 99, 100),
            (100, 101, 89, 90),  # target for short = 100-10=90, touched via low
        ])
        label = label_entry(candles, 0, direction="short", target_distance=10, stop_distance=5, horizon_bars=1)
        assert label.target_event is True


class TestIndependence:
    """The core requirement: STOP_SAFETY_EVENT is NOT `not target_event`."""

    def test_target_hit_early_then_stop_breached_later_in_same_horizon(self):
        """A trade that WINS the target race can still fail the
        full-horizon stop-safety check -- proving stop_safety_event is
        not determined by target_event alone (never simply `1 - P_target`)."""
        candles = _candles([
            (100, 101, 99, 100),   # entry
            (100, 111, 99, 108),   # target (110) touched here -- race decided WIN
            (108, 109, 94, 96),    # price later whips back and breaches stop (95) too
        ])
        label = label_entry(candles, 0, direction="long", target_distance=10, stop_distance=5, horizon_bars=2)
        assert label.target_event is True          # won the race
        assert label.stop_safety_event is False     # but stop WAS touched later in the same horizon

    def test_target_hit_and_stop_never_touched_is_the_other_reachable_outcome(self):
        """Same target_event=True outcome, but here the stop distance is
        never breached -- showing BOTH stop_safety_event values are
        reachable from target_event=True, i.e. stop_safety_event carries
        real independent information rather than being implied by
        target_event."""
        candles = _candles([
            (100, 101, 99, 100),
            (100, 111, 99, 108),   # target hit, race decided WIN
            (108, 109, 105, 107),  # stays well clear of the stop (95) afterward
        ])
        label = label_entry(candles, 0, direction="long", target_distance=10, stop_distance=5, horizon_bars=2)
        assert label.target_event is True
        assert label.stop_safety_event is True


class TestTruncation:
    def test_truncated_when_archive_runs_out_before_horizon(self):
        candles = _candles([
            (100, 101, 99, 100),
            (100, 103, 98, 101),
        ])
        label = label_entry(candles, 0, direction="long", target_distance=10, stop_distance=5, horizon_bars=10)
        assert label.truncated is True

    def test_not_truncated_when_full_horizon_available(self):
        candles = _candles([(100, 101, 99, 100)] * 5)
        label = label_entry(candles, 0, direction="long", target_distance=10, stop_distance=5, horizon_bars=3)
        assert label.truncated is False


class TestEdgeCases:
    def test_returns_none_for_empty_candles(self):
        assert label_entry(pd.DataFrame(), 0, direction="long", target_distance=10, stop_distance=5, horizon_bars=3) is None

    def test_returns_none_when_entry_idx_past_end(self):
        candles = _candles([(100, 101, 99, 100)] * 3)
        assert label_entry(candles, 5, direction="long", target_distance=10, stop_distance=5, horizon_bars=3) is None

    def test_rejects_non_positive_distances(self):
        candles = _candles([(100, 101, 99, 100)] * 3)
        with pytest.raises(ValueError):
            label_entry(candles, 0, direction="long", target_distance=0, stop_distance=5, horizon_bars=3)
        with pytest.raises(ValueError):
            label_entry(candles, 0, direction="long", target_distance=10, stop_distance=-1, horizon_bars=3)

    def test_mfe_mae_are_non_negative_and_reflect_full_range(self):
        candles = _candles([
            (100, 101, 99, 100),
            (100, 106, 97, 103),   # favorable move (mfe candidate)
            (103, 104, 90, 91),    # adverse move (mae candidate)
        ])
        label = label_entry(candles, 0, direction="long", target_distance=10, stop_distance=5, horizon_bars=2)
        assert label.mfe >= 0
        assert label.mae >= 0
        assert label.mae >= 9  # entry 100, low 90 in last bar -> adverse excursion of ~10
