"""
test_agents/trading_intelligence/test_structure_chart.py -- Milestone 20,
Phase 3: regression tests for agents/trading_intelligence/structure_chart.py.
Real matplotlib renders (fast, headless Agg backend) -- these tests check
files actually get written and cleaned up, not just that no exception was
raised.
"""
import glob
import os

import pytest

from agents.trading_intelligence import structure_chart as sc


def _candles(n=30, start=100.0, step=0.5):
    return [{"open": start + i * step, "high": start + i * step + 1, "low": start + i * step - 1,
             "close": start + i * step + 0.3, "volume": 1000} for i in range(n)]


@pytest.fixture(autouse=True)
def _cleanup_charts():
    pattern = os.path.join(sc.CHART_DIR, "**", "*.jpg")
    before = set(glob.glob(pattern, recursive=True))
    yield
    after = set(glob.glob(pattern, recursive=True))
    for f in after - before:
        os.remove(f)


REVERSAL = {
    "level": 100, "previous_role": "RESISTANCE", "current_role": "SUPPORT", "confidence": 89,
    "breakout_candle": {"high": 110, "low": 95, "close": 109},
    "retest_candle": {"high": 108, "low": 99.5, "close": 107},
}
OVERLAY = {"direction": "BULLISH", "entry": 115, "sl": 94.5, "t1": 135.5, "t2": 156}


class TestRenderStructureChart:
    def test_returns_a_real_existing_jpeg_path(self):
        path = sc.render_structure_chart("NIFTY", _candles(), level=100, reversal=REVERSAL, overlay=OVERLAY, confidence=89)
        assert path is not None
        assert os.path.exists(path)
        assert path.endswith(".jpg")
        assert os.path.getsize(path) > 0

    def test_filename_includes_symbol(self):
        path = sc.render_structure_chart("BANKNIFTY", _candles(), level=56000, reversal=REVERSAL)
        assert "BANKNIFTY" in os.path.basename(path)

    def test_returns_none_for_empty_candles(self):
        assert sc.render_structure_chart("NIFTY", [], level=100, reversal=REVERSAL) is None

    def test_works_without_a_reversal_or_overlay(self):
        # BREAKOUT_WATCH/REVERSAL_RISK alerts don't have a confirmed
        # role-flip reversal -- must still render a chart, just without
        # the breakout-arrow/retest-zone markup.
        path = sc.render_structure_chart("NIFTY", _candles(), level=100, state="BREAKOUT_WATCH")
        assert path is not None
        assert os.path.exists(path)

    def test_works_without_an_overlay(self):
        path = sc.render_structure_chart("NIFTY", _candles(), level=100, reversal=REVERSAL, overlay=None)
        assert path is not None

    def test_never_raises_on_malformed_candles(self):
        # Missing required keys -- must degrade to None, never raise
        # (a charting bug must never break the real alert send).
        bad_candles = [{"open": 1}]
        assert sc.render_structure_chart("NIFTY", bad_candles, level=100, reversal=REVERSAL) is None

    def test_max_candles_shown_caps_rendering_input(self):
        # Just confirms a large candle series doesn't error and still
        # produces a real file -- not asserting on internal pixel data.
        path = sc.render_structure_chart("NIFTY", _candles(n=500), level=100, reversal=REVERSAL)
        assert path is not None
        assert os.path.exists(path)


class TestPreviewMode:
    def test_preview_saves_to_the_previews_subdirectory(self):
        path = sc.render_structure_chart("CRUDEOIL", _candles(), level=8000, state="RANGE", confidence=35, preview=True)
        assert path is not None
        assert path.startswith(sc.PREVIEW_DIR)
        assert os.path.exists(path)

    def test_non_preview_saves_to_the_main_directory_not_previews(self):
        path = sc.render_structure_chart("NIFTY", _candles(), level=100, reversal=REVERSAL, preview=False)
        assert not path.startswith(sc.PREVIEW_DIR)
        assert path.startswith(sc.CHART_DIR)
