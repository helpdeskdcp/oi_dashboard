"""
test_agents/trading_intelligence/test_structure_tuning.py -- Milestone
20, Phase 7: regression tests for structure_tuning.py, the bounded/
rate-limited/audited autonomous half of the adaptive learning loop.
Mocks structure_backtest.backtest_parameters()/data_access.load_candles()
throughout -- this file tests the TUNING DECISION logic (bounds, sample
size, improvement margin, cooldown, audit log), not the backtest math
itself (see test_structure_backtest.py for that).
"""
import datetime as dt
import types

import pandas as pd
import pytest

import institutional_levels as il
from agents.trading_intelligence import structure_backtest, structure_tuning as st


@pytest.fixture(autouse=True)
def _isolate_db(monkeypatch, tmp_path):
    monkeypatch.setattr(st, "DB_PATH", str(tmp_path / "tuning_test.db"))
    st.init_db()
    # Every test restores the live constants it touches -- these are
    # REAL module globals structure_alerts.py's live detection also
    # reads, so a test that mutates them must never leak into another.
    monkeypatch.setattr(il, "MAX_RETEST_CANDLES", 3)
    monkeypatch.setattr(il, "MIN_VOLUME_MULTIPLIER", 1.2)


def _fake_result(*, wins, losses):
    return types.SimpleNamespace(wins=wins, losses=losses)


def _mock_backtest(monkeypatch, win_rate_by_params: dict, *, sample_size=50):
    """win_rate_by_params: {(max_retest_candles, min_volume_multiplier): win_rate}.
    Every symbol contributes the SAME synthetic wins/losses for a given
    param combo (keeps the aggregate math simple and predictable)."""
    def fake_backtest_parameters(symbol, candles, *, max_retest_candles, min_volume_multiplier):
        key = (max_retest_candles, min_volume_multiplier)
        rate = win_rate_by_params.get(key)
        if rate is None:
            return _fake_result(wins=0, losses=0)
        wins = round(sample_size * rate)
        return _fake_result(wins=wins, losses=sample_size - wins)

    monkeypatch.setattr(structure_backtest, "backtest_parameters", fake_backtest_parameters)
    monkeypatch.setattr(structure_backtest.data_access, "load_candles",
                         lambda sym, **kw: pd.DataFrame([{"datetime": dt.datetime.now()}]))


class TestEvaluateAndMaybeTune:
    def test_dry_run_never_mutates_live_constants(self, monkeypatch):
        _mock_backtest(monkeypatch, {
            (2, 1.2): 0.40, (3, 1.2): 0.45, (4, 1.2): 0.70, (5, 1.2): 0.42,
            (3, 1.0): 0.44, (3, 1.5): 0.43, (3, 1.8): 0.41, (3, 2.0): 0.40,
        })
        before = il.MAX_RETEST_CANDLES
        st.evaluate_and_maybe_tune(["NIFTY"], apply=False, force=True)
        assert il.MAX_RETEST_CANDLES == before

    def test_applies_when_a_candidate_clearly_beats_current(self, monkeypatch):
        _mock_backtest(monkeypatch, {
            (2, 1.2): 0.40, (3, 1.2): 0.45, (4, 1.2): 0.70, (5, 1.2): 0.42,
            (3, 1.0): 0.44, (3, 1.5): 0.43, (3, 1.8): 0.41, (3, 2.0): 0.40,
        })
        result = st.evaluate_and_maybe_tune(["NIFTY"], apply=True, force=True)
        assert il.MAX_RETEST_CANDLES == 4
        decision = next(d for d in result["decisions"] if d["parameter"] == "max_retest_candles")
        assert decision["applied"] is True
        assert decision["best_candidate"] == 4

    def test_does_not_apply_when_improvement_is_below_the_margin(self, monkeypatch):
        # Best alternative only 2 points better than current -- below
        # TUNING_MIN_IMPROVEMENT (5 points).
        _mock_backtest(monkeypatch, {
            (2, 1.2): 0.40, (3, 1.2): 0.45, (4, 1.2): 0.47, (5, 1.2): 0.42,
            (3, 1.0): 0.44, (3, 1.5): 0.43, (3, 1.8): 0.41, (3, 2.0): 0.40,
        })
        result = st.evaluate_and_maybe_tune(["NIFTY"], apply=True, force=True)
        assert il.MAX_RETEST_CANDLES == 3   # unchanged
        decision = next(d for d in result["decisions"] if d["parameter"] == "max_retest_candles")
        assert decision["applied"] is False
        assert "doesn't beat current" in decision["reason"]

    def test_does_not_apply_below_minimum_sample_size(self, monkeypatch):
        _mock_backtest(monkeypatch, {
            (2, 1.2): 0.40, (3, 1.2): 0.45, (4, 1.2): 0.90, (5, 1.2): 0.42,
            (3, 1.0): 0.44, (3, 1.5): 0.43, (3, 1.8): 0.41, (3, 2.0): 0.40,
        }, sample_size=5)   # below TUNING_MIN_SAMPLE_SIZE (30)
        result = st.evaluate_and_maybe_tune(["NIFTY"], apply=True, force=True)
        assert il.MAX_RETEST_CANDLES == 3   # unchanged
        decision = next(d for d in result["decisions"] if d["parameter"] == "max_retest_candles")
        assert decision["applied"] is False
        assert "insufficient sample" in decision["reason"]

    def test_candidate_lists_never_exceed_their_own_bounds(self):
        # Every TUNABLE_PARAMS entry's own "candidates" must already sit
        # inside its "bounds" -- the real safety guarantee comes from
        # this invariant holding, not from a runtime filter that could
        # silently mask a config mistake.
        for spec in st.TUNABLE_PARAMS.values():
            lo, hi = spec["bounds"]
            assert all(lo <= c <= hi for c in spec["candidates"])

    def test_every_evaluation_is_logged_even_when_not_applied(self, monkeypatch):
        _mock_backtest(monkeypatch, {
            (2, 1.2): 0.40, (3, 1.2): 0.45, (4, 1.2): 0.47, (5, 1.2): 0.42,
            (3, 1.0): 0.44, (3, 1.5): 0.43, (3, 1.8): 0.41, (3, 2.0): 0.40,
        })
        st.evaluate_and_maybe_tune(["NIFTY"], apply=True, force=True)
        history = st.list_tuning_history()
        assert len(history) == 2   # one row per TUNABLE_PARAMS entry
        assert {h["parameter"] for h in history} == set(st.TUNABLE_PARAMS.keys())

    def test_cooldown_blocks_a_second_change_within_the_window(self, monkeypatch):
        _mock_backtest(monkeypatch, {
            (2, 1.2): 0.40, (3, 1.2): 0.45, (4, 1.2): 0.70, (5, 1.2): 0.42,
            (3, 1.0): 0.44, (3, 1.5): 0.43, (3, 1.8): 0.41, (3, 2.0): 0.40,
        })
        now = dt.datetime(2026, 8, 14, 9, 0)
        st.evaluate_and_maybe_tune(["NIFTY"], apply=True, force=True, now=now)
        assert il.MAX_RETEST_CANDLES == 4

        # A very different backtest landscape 2 days later -- even
        # though 2 would now look best, the cooldown must still block
        # a second change to this parameter within TUNING_COOLDOWN_DAYS.
        _mock_backtest(monkeypatch, {
            (2, 1.2): 0.90, (3, 1.2): 0.45, (4, 1.2): 0.40, (5, 1.2): 0.42,
            (3, 1.0): 0.44, (3, 1.5): 0.43, (3, 1.8): 0.41, (3, 2.0): 0.40,
        })
        later = now + dt.timedelta(days=2)
        st.evaluate_and_maybe_tune(["NIFTY"], apply=True, force=True, now=later)
        assert il.MAX_RETEST_CANDLES == 4   # still unchanged -- cooldown active

    def test_change_is_allowed_again_after_the_cooldown_elapses(self, monkeypatch):
        _mock_backtest(monkeypatch, {
            (2, 1.2): 0.40, (3, 1.2): 0.45, (4, 1.2): 0.70, (5, 1.2): 0.42,
            (3, 1.0): 0.44, (3, 1.5): 0.43, (3, 1.8): 0.41, (3, 2.0): 0.40,
        })
        now = dt.datetime(2026, 8, 14, 9, 0)
        st.evaluate_and_maybe_tune(["NIFTY"], apply=True, force=True, now=now)
        assert il.MAX_RETEST_CANDLES == 4

        _mock_backtest(monkeypatch, {
            (2, 1.2): 0.90, (4, 1.2): 0.40, (3, 1.2): 0.41, (5, 1.2): 0.42,
            (4, 1.0): 0.44, (4, 1.5): 0.43, (4, 1.8): 0.41, (4, 2.0): 0.40,
        })
        later = now + dt.timedelta(days=8)   # past TUNING_COOLDOWN_DAYS (7)
        st.evaluate_and_maybe_tune(["NIFTY"], apply=True, force=True, now=later)
        assert il.MAX_RETEST_CANDLES == 2

    def test_second_call_without_force_is_skipped_within_the_evaluation_interval(self, monkeypatch):
        _mock_backtest(monkeypatch, {
            (2, 1.2): 0.40, (3, 1.2): 0.45, (4, 1.2): 0.70, (5, 1.2): 0.42,
            (3, 1.0): 0.44, (3, 1.5): 0.43, (3, 1.8): 0.41, (3, 2.0): 0.40,
        })
        now = dt.datetime(2026, 8, 14, 9, 0)
        st.evaluate_and_maybe_tune(["NIFTY"], apply=True, now=now)
        soon_after = now + dt.timedelta(hours=1)
        result = st.evaluate_and_maybe_tune(["NIFTY"], apply=True, now=soon_after)
        assert result["ran"] is False


class TestListTuningHistory:
    def test_empty_history_returns_empty_list(self):
        assert st.list_tuning_history() == []

    def test_filters_by_parameter(self, monkeypatch):
        _mock_backtest(monkeypatch, {
            (2, 1.2): 0.40, (3, 1.2): 0.45, (4, 1.2): 0.47, (5, 1.2): 0.42,
            (3, 1.0): 0.44, (3, 1.5): 0.43, (3, 1.8): 0.41, (3, 2.0): 0.40,
        })
        st.evaluate_and_maybe_tune(["NIFTY"], apply=True, force=True)
        history = st.list_tuning_history(parameter="max_retest_candles")
        assert len(history) == 1
        assert history[0]["parameter"] == "max_retest_candles"
