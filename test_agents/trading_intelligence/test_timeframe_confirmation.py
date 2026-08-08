import pandas as pd
import pytest

from agents.trading_intelligence import multi_timeframe
from agents.trading_intelligence import timeframe_confirmation as tc


def _candles(closes: list) -> pd.DataFrame:
    return pd.DataFrame({
        "datetime": pd.date_range("2026-08-06 09:15", periods=len(closes), freq="15min"),
        "open": closes, "high": closes, "low": closes, "close": closes,
    })


class TestBarTrend:
    def test_unknown_when_too_few_bars(self, ti_db):
        assert tc._bar_trend(_candles([100, 101, 102])) == "UNKNOWN"  # need lookback+1=5

    def test_unknown_when_candles_none_or_empty(self, ti_db):
        assert tc._bar_trend(None) == "UNKNOWN"
        assert tc._bar_trend(pd.DataFrame()) == "UNKNOWN"

    def test_up_when_close_rose_over_the_lookback_window(self, ti_db):
        assert tc._bar_trend(_candles([100, 100, 100, 100, 105])) == "UP"

    def test_down_when_close_fell_over_the_lookback_window(self, ti_db):
        assert tc._bar_trend(_candles([105, 104, 103, 102, 100])) == "DOWN"

    def test_flat_when_close_is_unchanged(self, ti_db):
        assert tc._bar_trend(_candles([100, 101, 99, 102, 100])) == "FLAT"

    def test_only_the_lookback_window_matters_not_older_history(self, ti_db):
        # 20 bars of noise, but the LAST 5 (lookback=4) rose cleanly.
        closes = [100, 90, 110, 95, 105] + [200, 201, 202, 203, 210]
        assert tc._bar_trend(_candles(closes)) == "UP"

    def test_unknown_when_the_starting_close_is_nan(self, ti_db):
        """Phase 7 validation fix: NaN is truthy in Python, so `not start`
        alone does not catch a NaN close (a real data-quality gap in the
        archived candle file) -- it must not silently fall through to
        FLAT, which would dilute alignment_score's denominator with a
        reading that was never really available."""
        closes = [float("nan"), 101, 102, 103, 104]
        assert tc._bar_trend(_candles(closes)) == "UNKNOWN"

    def test_unknown_when_the_ending_close_is_nan(self, ti_db):
        closes = [100, 101, 102, 103, float("nan")]
        assert tc._bar_trend(_candles(closes)) == "UNKNOWN"


class TestCheckValidation:
    def test_rejects_invalid_direction(self, ti_db):
        with pytest.raises(ValueError):
            tc.check("NIFTY", direction="LONG")


class TestCheckIntegration:
    """Integration coverage against the REAL archived NIFTY candle data
    (same convention test_multi_timeframe.py's own TestGetTimeframe suite
    already established) -- never synthetic/fabricated market data."""

    def test_returns_a_reading_per_derivable_timeframe(self, ti_db):
        alignment = tc.check("NIFTY", direction="CE")
        assert {r.timeframe for r in alignment.readings} == set(multi_timeframe.DERIVABLE_TIMEFRAMES)

    def test_alignment_score_is_a_real_bounded_ratio_when_available(self, ti_db):
        alignment = tc.check("NIFTY", direction="CE")
        if alignment.available_count > 0:
            assert 0.0 <= alignment.alignment_score <= 100.0
            assert alignment.alignment_label in ("CONFIRMED", "MIXED", "CONTRARY")
        else:
            assert alignment.alignment_score is None
            assert alignment.alignment_label == "UNKNOWN"

    def test_agreement_count_never_exceeds_available_count(self, ti_db):
        alignment = tc.check("NIFTY", direction="PE")
        assert alignment.agreement_count <= alignment.available_count

    def test_ce_and_pe_are_never_both_fully_confirmed_for_the_same_real_data(self, ti_db):
        """A real, meaningful sanity check (not just a shape check): the
        SAME real archive data cannot simultaneously show the market
        trending up enough to CONFIRM a CE read AND down enough to
        CONFIRM a PE read -- CE's agreement_count and PE's agreement_count
        together can never exceed available_count (each available
        timeframe agrees with AT MOST one direction, since UP/DOWN/FLAT
        are mutually exclusive per timeframe)."""
        ce = tc.check("NIFTY", direction="CE")
        pe = tc.check("NIFTY", direction="PE")
        assert ce.available_count == pe.available_count
        assert ce.agreement_count + pe.agreement_count <= ce.available_count

    def test_unknown_symbol_degrades_honestly_never_raises(self, ti_db):
        alignment = tc.check("NOT_A_REAL_SYMBOL", direction="CE")
        assert alignment.available_count == 0
        assert alignment.alignment_score is None
        assert alignment.alignment_label == "UNKNOWN"
        assert all(r.available is False and r.reason for r in alignment.readings)

    def test_every_unavailable_reading_carries_an_honest_reason(self, ti_db):
        alignment = tc.check("NOT_A_REAL_SYMBOL", direction="PE")
        for r in alignment.readings:
            if not r.available:
                assert r.reason is not None and r.reason != ""


class TestStress:
    """Milestone 11 Plan's own success criterion for this module: 'a
    stress test across a wide multi-strike chain confirms it never raises
    when 15m/1h data is unavailable' -- exercised here across every
    direction and several symbols (available and unavailable) in one
    sweep, matching M10's own TestStress shape."""

    def test_never_raises_across_symbols_and_directions(self, ti_db):
        symbols = ("NIFTY", "BANKNIFTY", "SENSEX", "NOT_A_REAL_SYMBOL", "ANOTHER_FAKE_ONE")
        for symbol in symbols:
            for direction in ("CE", "PE"):
                alignment = tc.check(symbol, direction=direction)
                assert alignment.symbol == symbol
                assert alignment.direction == direction
                assert len(alignment.readings) == len(multi_timeframe.DERIVABLE_TIMEFRAMES)

