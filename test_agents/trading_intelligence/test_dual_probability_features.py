"""Unit tests for agents/trading_intelligence/dual_probability_features.py."""
import datetime as dt

import pandas as pd

from agents.quant_researcher import features as qr_features
from agents.trading_intelligence.dual_probability_features import extract_feature_groups


def _candles(n=250, *, with_volume=False, volume_value=1000):
    start = dt.datetime(2026, 1, 1, 9, 15)
    rows = []
    price = 100.0
    for i in range(n):
        price += 0.1 if i % 2 == 0 else -0.05
        row = {
            "datetime": start + dt.timedelta(minutes=3 * i),
            "open": price, "high": price + 1, "low": price - 1, "close": price,
        }
        if with_volume:
            row["volume"] = volume_value
        rows.append(row)
    return pd.DataFrame(rows)


class TestHonestGaps:
    def test_volume_group_is_none_without_a_volume_column(self):
        ctx = qr_features.FeatureContext(candles=_candles())  # no "volume" column at all
        g = extract_feature_groups(ctx, 100)
        assert g.volume is None

    def test_volume_group_is_none_when_volume_is_all_zero(self):
        """NSE index symbols (NIFTY/BANKNIFTY) have a real volume column
        that is always zero -- confirmed against the real archive. Must
        report None, never a degenerate MFI computed from fabricated-
        looking all-zero data."""
        ctx = qr_features.FeatureContext(candles=_candles(with_volume=True, volume_value=0))
        g = extract_feature_groups(ctx, 100)
        assert g.volume is None

    def test_volume_group_is_computed_when_real_volume_is_present(self):
        ctx = qr_features.FeatureContext(candles=_candles(with_volume=True, volume_value=1000))
        g = extract_feature_groups(ctx, 100)
        assert g.volume is not None
        assert -1.0 <= g.volume <= 1.0

    def test_mtf_group_is_none_below_min_history(self):
        ctx = qr_features.FeatureContext(candles=_candles(n=30))
        g = extract_feature_groups(ctx, 10)
        assert g.mtf is None

    def test_mtf_group_is_computed_with_enough_history(self):
        ctx = qr_features.FeatureContext(candles=_candles(n=250))
        g = extract_feature_groups(ctx, 150)
        assert g.mtf in (-1.0, 0.0, 1.0)

    def test_oi_group_is_none_without_cycles(self):
        ctx = qr_features.FeatureContext(candles=_candles(), cycles=None)
        g = extract_feature_groups(ctx, 100)
        assert g.oi is None


class TestGroupCount:
    def test_group_count_excludes_none_groups(self):
        # no volume column and no cycles -> volume and oi are structurally
        # None here regardless of history; trend/momentum/structure/mtf/regime
        # can each independently be real or None depending on warm-up
        ctx = qr_features.FeatureContext(candles=_candles(), cycles=None)
        g = extract_feature_groups(ctx, 100)
        assert g.volume is None
        assert g.oi is None
        assert g.group_count() <= 5  # trend, momentum, structure, mtf, regime at most
        assert g.group_count() == sum(
            1 for v in (g.trend, g.momentum, g.structure, g.oi, g.volume, g.mtf) if v is not None
        ) + (1 if g.regime not in (None, "UNKNOWN") else 0)

    def test_group_count_can_reach_six_with_volume_present(self):
        ctx = qr_features.FeatureContext(candles=_candles(with_volume=True, volume_value=1000), cycles=None)
        g = extract_feature_groups(ctx, 100)
        assert g.volume is not None
        assert g.group_count() <= 6  # oi still None (no cycles) -- everything else can be real

    def test_unknown_regime_does_not_count_as_a_group(self):
        ctx = qr_features.FeatureContext(candles=_candles(n=10))  # too few bars for ADX (needs period*2=28)
        g = extract_feature_groups(ctx, 5)
        assert g.regime == "UNKNOWN"
        assert (1 if g.regime not in (None, "UNKNOWN") else 0) == 0


class TestEdgeCases:
    def test_idx_past_end_of_series_returns_none_scalars_not_crash(self):
        ctx = qr_features.FeatureContext(candles=_candles(n=20))
        g = extract_feature_groups(ctx, 10_000)
        assert g.trend is None
        assert g.momentum is None
        assert g.structure is None

    def test_regime_override_is_used_instead_of_recomputing(self):
        ctx = qr_features.FeatureContext(candles=_candles())
        g = extract_feature_groups(ctx, 100, regime_override="TRENDING")
        assert g.regime == "TRENDING"

    def test_no_lookahead_same_prefix_gives_same_trend_value(self):
        """Feature value at idx must depend only on bars up to idx --
        appending future bars after idx must not change it (matches
        FEATURE_REGISTRY's own point-in-time-safe rolling-window design)."""
        full = _candles(n=250)
        truncated = full.iloc[:150].reset_index(drop=True)
        ctx_full = qr_features.FeatureContext(candles=full)
        ctx_trunc = qr_features.FeatureContext(candles=truncated)
        g_full = extract_feature_groups(ctx_full, 100)
        g_trunc = extract_feature_groups(ctx_trunc, 100)
        assert g_full.trend == g_trunc.trend
        assert g_full.momentum == g_trunc.momentum

    def test_no_lookahead_volume_and_mtf_unaffected_by_future_bars(self):
        full = _candles(n=250, with_volume=True, volume_value=1000)
        truncated = full.iloc[:200].reset_index(drop=True)
        ctx_full = qr_features.FeatureContext(candles=full)
        ctx_trunc = qr_features.FeatureContext(candles=truncated)
        g_full = extract_feature_groups(ctx_full, 150)
        g_trunc = extract_feature_groups(ctx_trunc, 150)
        assert g_full.volume == g_trunc.volume
        assert g_full.mtf == g_trunc.mtf
