"""
test_agents/quant_researcher/test_features.py -- regression tests for
agents/quant_researcher/features.py's plug-in indicator library.
"""
import numpy as np
import pandas as pd
import pytest

from agents.quant_researcher import features


class TestGracefulDegradation:
    def test_ohlcv_features_return_nan_series_on_empty_candles(self, empty_candles):
        ctx = features.FeatureContext(candles=empty_candles)
        for name in ("atr", "vwap_deviation", "range_compression", "momentum_exhaustion",
                     "liquidity_sweep", "cpr_width", "cpr_position", "expiry_flag"):
            series = features.compute_feature(name, ctx)
            assert len(series) == 0

    def test_cycle_features_return_nan_when_no_cycles_supplied(self, trending_candles):
        ctx = features.FeatureContext(candles=trending_candles, cycles=None)
        for name in ("oi_delta_bias", "gamma_exposure", "premium_expansion",
                     "max_pain_distance", "institutional_activity", "iv_crush"):
            series = features.compute_feature(name, ctx)
            assert len(series) == len(trending_candles)
            assert series.isna().all()

    def test_expiry_flag_all_zero_without_a_calendar(self, trending_candles):
        ctx = features.FeatureContext(candles=trending_candles, expiry_dates=None)
        series = features.compute_feature("expiry_flag", ctx)
        assert (series == 0.0).all()

    def test_unknown_feature_name_raises(self, trending_candles):
        ctx = features.FeatureContext(candles=trending_candles)
        with pytest.raises(KeyError):
            features.compute_feature("not_a_real_feature", ctx)


class TestOhlcvFeatures:
    def test_atr_is_nonnegative_and_aligned(self, trending_candles):
        ctx = features.FeatureContext(candles=trending_candles)
        series = features.atr(ctx)
        assert len(series) == len(trending_candles)
        assert (series.dropna() >= 0).all()

    def test_vwap_deviation_positive_when_trending_up(self, trending_candles):
        ctx = features.FeatureContext(candles=trending_candles)
        series = features.vwap_deviation(ctx)
        # a steadily rising close should end up above its own trailing VWAP
        assert series.iloc[-5:].mean() > 0

    def test_range_compression_ratio_of_flat_series_near_one(self, flat_candles):
        ctx = features.FeatureContext(candles=flat_candles)
        series = features.range_compression(ctx)
        assert series.iloc[-1] == pytest.approx(1.0, abs=0.5)

    def test_liquidity_sweep_detects_a_wick_below_prior_low_that_closes_back_above(self):
        # 10 flat bars around 100, then one bar wicks to 90 but closes at 100.5
        rows = []
        base_ts = pd.Timestamp("2026-05-04 09:15:00")
        for i in range(10):
            rows.append({"datetime": base_ts + pd.Timedelta(minutes=3 * i),
                         "open": 100.0, "high": 100.5, "low": 99.5, "close": 100.0, "volume": 100})
        rows.append({"datetime": base_ts + pd.Timedelta(minutes=30),
                     "open": 100.0, "high": 100.6, "low": 90.0, "close": 100.5, "volume": 100})
        candles = pd.DataFrame(rows)
        ctx = features.FeatureContext(candles=candles)
        series = features.liquidity_sweep(ctx)
        assert series.iloc[-1] > 0

    def test_cpr_width_and_position_are_finite_over_multiple_days(self, trending_candles):
        ctx = features.FeatureContext(candles=trending_candles)
        width = features.cpr_width(ctx)
        position = features.cpr_position(ctx)
        assert len(width) == len(trending_candles)
        assert len(position) == len(trending_candles)
        assert np.isfinite(width.dropna()).all()

    def test_expiry_flag_marks_only_the_supplied_dates(self, trending_candles):
        target_date = trending_candles["datetime"].iloc[0].strftime("%Y-%m-%d")
        ctx = features.FeatureContext(candles=trending_candles, expiry_dates={target_date})
        series = features.compute_feature("expiry_flag", ctx)
        on_date = trending_candles["datetime"].dt.strftime("%Y-%m-%d") == target_date
        assert (series[on_date] == 1.0).all()
        assert (series[~on_date] == 0.0).all()


class TestCycleFeatures:
    def test_oi_delta_bias_is_aligned_and_finite(self, trending_candles, cycles_for):
        ctx = features.FeatureContext(candles=trending_candles, cycles=cycles_for)
        series = features.oi_delta_bias(ctx)
        assert len(series) == len(trending_candles)
        assert series.notna().any()

    def test_max_pain_distance_matches_manual_calc_for_first_cycle(self, trending_candles, cycles_for):
        ctx = features.FeatureContext(candles=trending_candles, cycles=cycles_for)
        series = features.max_pain_distance(ctx)
        first_cycle = cycles_for[0]
        expected = (first_cycle["underlying_ltp"] - first_cycle["max_pain"]) / first_cycle["underlying_ltp"]
        assert series.iloc[0] == pytest.approx(expected, rel=1e-6)

    def test_gamma_exposure_and_institutional_activity_are_positive(self, trending_candles, cycles_for):
        ctx = features.FeatureContext(candles=trending_candles, cycles=cycles_for)
        assert (features.gamma_exposure(ctx).dropna() >= 0).all()
        assert (features.institutional_activity(ctx).dropna() >= 0).all()
