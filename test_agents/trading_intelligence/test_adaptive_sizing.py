import random

import position_sizing
from agents.trading_intelligence import adaptive_sizing as ads
from agents.trading_intelligence import regime_profile as rp
from agents.trading_intelligence import ti_store as ts
from agents.trading_intelligence import timeframe_confirmation as tc


class _Regime:
    def __init__(self, trend_regime):
        self.trend_regime = trend_regime


class _Alignment:
    def __init__(self, alignment_score):
        self.alignment_score = alignment_score


class TestSetupStrength:
    def test_no_evidence_is_none(self, ti_db):
        strength, components = ads._setup_strength()
        assert strength is None
        assert components == {"regime": None, "timeframe": None, "institutional": None}

    def test_regime_only(self, ti_db):
        strength, components = ads._setup_strength(regime=_Regime("TRENDING"))
        assert strength == 100.0
        assert components["regime"] == 100.0

    def test_unknown_regime_is_excluded(self, ti_db):
        strength, components = ads._setup_strength(regime=_Regime("UNKNOWN"))
        assert strength is None

    def test_averages_available_components(self, ti_db):
        strength, components = ads._setup_strength(
            regime=_Regime("RANGING"), alignment=_Alignment(80.0), institutional_backed=True,
        )
        assert components == {"regime": 40.0, "timeframe": 80.0, "institutional": 100.0}
        assert strength == round((40.0 + 80.0 + 100.0) / 3, 1)

    def test_institutional_false_is_zero_not_excluded(self, ti_db):
        strength, components = ads._setup_strength(institutional_backed=False)
        assert components["institutional"] == 0.0
        assert strength == 0.0


class TestSetupMultiplier:
    def test_no_evidence_is_neutral(self, ti_db):
        assert ads._setup_multiplier(None) == 1.0

    def test_zero_strength_is_the_floor(self, ti_db):
        assert ads._setup_multiplier(0.0) == ads.MIN_SETUP_MULTIPLIER

    def test_full_strength_is_the_ceiling(self, ti_db):
        assert ads._setup_multiplier(100.0) == ads.MAX_SETUP_MULTIPLIER

    def test_ceiling_is_never_above_one(self, ti_db):
        assert ads.MAX_SETUP_MULTIPLIER <= 1.0

    def test_monotonically_increasing_with_strength(self, ti_db):
        assert ads._setup_multiplier(20.0) < ads._setup_multiplier(80.0)


class TestTrackRecordMultiplier:
    def test_no_setup_strength_is_neutral(self, ti_db):
        mult, reason = ads._track_record_multiplier(None)
        assert mult == 1.0
        assert "no setup-strength evidence" in reason

    def test_insufficient_sample_is_neutral(self, ti_db):
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=130.0, sl_price=85.0, qty=50, regime_trend_at_entry="TRENDING")
        ts.close_trade(tid, exit_price=130.0, exit_reason="TARGET HIT")
        mult, reason = ads._track_record_multiplier(100.0, min_sample=5)
        assert mult == 1.0
        assert "insufficient" in reason

    def test_strong_track_record_stays_neutral_never_scales_above_one(self, ti_db):
        for _ in range(6):
            tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                                 target_price=130.0, sl_price=85.0, qty=50, regime_trend_at_entry="TRENDING")
            ts.close_trade(tid, exit_price=130.0, exit_reason="TARGET HIT")
        mult, reason = ads._track_record_multiplier(100.0, min_sample=5)
        assert mult == 1.0
        assert "at or above neutral" in reason

    def test_poor_track_record_reduces_below_one(self, ti_db):
        for i in range(6):
            tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                                 target_price=130.0, sl_price=85.0, qty=50, regime_trend_at_entry="TRENDING")
            ts.close_trade(tid, exit_price=130.0 if i == 0 else 85.0,
                            exit_reason="TARGET HIT" if i == 0 else "STOP LOSS")
        mult, reason = ads._track_record_multiplier(100.0, min_sample=5)
        assert mult < 1.0
        assert mult >= ads.MIN_TRACK_RECORD_MULTIPLIER
        assert "reducing size" in reason

    def test_never_scales_above_one(self, ti_db):
        for _ in range(8):
            tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                                 target_price=130.0, sl_price=85.0, qty=50, regime_trend_at_entry="TRENDING")
            ts.close_trade(tid, exit_price=130.0, exit_reason="TARGET HIT")
        mult, _ = ads._track_record_multiplier(100.0, min_sample=5)
        assert mult <= 1.0


class TestCurrentLosingStreak:
    def test_no_trades_is_zero_streak(self, ti_db):
        assert ads._current_losing_streak([]) == (0, 0.0)

    def test_stops_at_the_first_win_from_the_front(self, ti_db):
        trades = [{"points": -50.0}, {"points": -30.0}, {"points": 100.0}, {"points": -20.0}]
        streak_len, loss = ads._current_losing_streak(trades)
        assert streak_len == 2
        assert loss == -80.0

    def test_a_win_at_the_front_is_zero_streak(self, ti_db):
        assert ads._current_losing_streak([{"points": 50.0}, {"points": -30.0}]) == (0, 0.0)


class TestStreakDampenerMultiplier:
    def test_insufficient_history_is_inactive(self, ti_db):
        mult, reason = ads._streak_dampener_multiplier(min_trades=10)
        assert mult == 1.0
        assert "insufficient" in reason

    def test_no_active_streak_is_inactive(self, ti_db):
        for i in range(10):
            tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                                 target_price=130.0, sl_price=85.0, qty=50)
            ts.close_trade(tid, exit_price=130.0, exit_reason="TARGET HIT")  # ends on a WIN
        mult, reason = ads._streak_dampener_multiplier(min_trades=10, rng=random.Random(1))
        assert mult == 1.0
        assert "no active losing streak" in reason

    def test_a_real_losing_streak_beyond_this_engines_own_worst_prior_drawdown_trips_the_dampener(self, ti_db):
        # Establish a real, mixed prior track record -- mostly small wins,
        # occasional small losses -- so there's a genuine, modest "worst
        # drawdown ever" baseline to compare against.
        for i in range(15):
            tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                                 target_price=110.0, sl_price=92.0, qty=50)
            ts.close_trade(tid, exit_price=110.0 if i % 4 != 0 else 92.0,
                            exit_reason="TARGET HIT" if i % 4 != 0 else "STOP LOSS")
        # ...then a real, severe, ongoing losing streak far outside that prior range.
        for _ in range(5):
            tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                                 target_price=105.0, sl_price=50.0, qty=50)
            ts.close_trade(tid, exit_price=50.0, exit_reason="STOP LOSS")

        for seed in range(5):  # robust across RNG seeds, not cherry-picked
            mult, reason = ads._streak_dampener_multiplier(min_trades=10, trials=200, percentile=75, rng=random.Random(seed))
            assert mult == ads.STREAK_DAMPENER_FACTOR, f"seed {seed} did not trip: {reason}"
            assert "reducing size" in reason

    def test_a_mild_streak_within_normal_range_does_not_trip_the_dampener(self, ti_db):
        # Same mixed-record prior history as the trip test (a real, modest
        # "worst drawdown ever" baseline)...
        for i in range(15):
            tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                                 target_price=110.0, sl_price=92.0, qty=50)
            ts.close_trade(tid, exit_price=110.0 if i % 4 != 0 else 92.0,
                            exit_reason="TARGET HIT" if i % 4 != 0 else "STOP LOSS")
        # ...then a single MILD loss, the same small magnitude as the prior
        # losses -- not a new, severe low.
        tid = ts.open_trade(symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
                             target_price=110.0, sl_price=92.0, qty=50)
        ts.close_trade(tid, exit_price=92.0, exit_reason="STOP LOSS")

        for seed in range(5):
            mult, reason = ads._streak_dampener_multiplier(min_trades=10, trials=200, percentile=75, rng=random.Random(seed))
            assert mult == 1.0, f"seed {seed} unexpectedly tripped: {reason}"
            assert "normal drawdown range" in reason


class TestComputeAdaptiveQuantity:
    def test_matches_risk_pct_baseline_with_no_evidence_at_all(self, ti_db):
        result = ads.compute_adaptive_quantity(100.0, 85.0, capital=500000.0, risk_pct=1.0, symbol="NIFTY")
        base = position_sizing.compute_quantity(100.0, 85.0, sizing_mode="risk_pct", capital=500000.0, risk_pct=1.0, min_qty=0)
        assert result.base_qty == base
        assert result.qty == base  # no evidence -> every multiplier neutral -> unchanged

    def test_never_exceeds_the_risk_pct_baseline_max_loss_guarantee(self, ti_db):
        """Plan's own Module 11.5 success criterion: the adaptive mode's
        output quantity must be provably bounded by the SAME max-loss
        guarantee risk_pct mode already has -- a direct comparison, not a
        smoke test -- across a spread of regime/alignment/institutional
        combinations, including maximally favorable ones."""
        base = position_sizing.compute_quantity(100.0, 85.0, sizing_mode="risk_pct", capital=500000.0, risk_pct=1.0, min_qty=0)
        scenarios = [
            {},
            {"regime": _Regime("TRENDING"), "alignment": _Alignment(100.0), "institutional_backed": True},
            {"regime": _Regime("RANGING"), "alignment": _Alignment(10.0), "institutional_backed": False},
            {"regime": _Regime("UNKNOWN")},
            {"institutional_backed": True},
        ]
        for kwargs in scenarios:
            result = ads.compute_adaptive_quantity(100.0, 85.0, capital=500000.0, risk_pct=1.0, symbol="NIFTY", **kwargs)
            assert result.qty <= base
            assert result.qty * abs(100.0 - 85.0) <= base * abs(100.0 - 85.0)  # max loss is bounded too

    def test_weak_setup_reduces_size_below_baseline(self, ti_db):
        result = ads.compute_adaptive_quantity(
            100.0, 85.0, capital=500000.0, risk_pct=1.0, symbol="NIFTY",
            regime=_Regime("RANGING"), institutional_backed=False,
        )
        assert result.qty < result.base_qty

    def test_min_qty_below_the_base_qty_ceiling_still_raises_the_floor(self, ti_db):
        result = ads.compute_adaptive_quantity(
            100.0, 85.0, capital=500000.0, risk_pct=1.0, symbol="NIFTY",
            regime=_Regime("RANGING"), institutional_backed=False, min_qty=5,
        )
        assert result.qty >= 5

    def test_min_qty_above_base_qty_is_capped_at_base_qty_not_honored(self, ti_db):
        """Phase 7 validation fix: a caller-supplied min_qty must never be
        able to push qty above base_qty -- base_qty IS the risk_pct
        max-loss bound this module exists to guarantee, so an absurd
        min_qty floor is capped there rather than silently overriding it
        (unlike position_sizing.compute_quantity()'s own min_qty, which
        makes no such guarantee to preserve)."""
        result = ads.compute_adaptive_quantity(
            100.0, 85.0, capital=500000.0, risk_pct=1.0, symbol="NIFTY",
            regime=_Regime("RANGING"), institutional_backed=False, min_qty=999999,
        )
        assert result.qty == result.base_qty
        assert result.qty < 999999

    def test_never_negative(self, ti_db):
        result = ads.compute_adaptive_quantity(100.0, 85.0, capital=0.0, risk_pct=1.0, symbol="NIFTY")
        assert result.qty >= 0


class TestAdaptiveSizingIntegration:
    """Against real archived data, the same convention every other M11
    module's integration tests already establish."""

    def test_runs_end_to_end_with_real_regime_and_alignment_reads(self, ti_db):
        from test_agents.trading_intelligence.conftest import insert_market_structure, insert_realistic_chain

        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        insert_market_structure(ti_db, symbol="NIFTY", ts="2026-08-06T10:00:00", adx=30.0)
        regime = rp.classify("NIFTY")
        alignment = tc.check("NIFTY", direction="CE")

        result = ads.compute_adaptive_quantity(
            100.0, 85.0, capital=500000.0, risk_pct=1.0, symbol="NIFTY", regime=regime, alignment=alignment,
        )
        assert result.qty >= 0
        assert result.qty <= result.base_qty


class TestStress:
    def test_never_raises_across_a_wide_range_of_inputs(self, ti_db):
        for regime in (None, _Regime("TRENDING"), _Regime("RANGING"), _Regime("UNKNOWN")):
            for alignment in (None, _Alignment(0.0), _Alignment(100.0)):
                for backed in (None, True, False):
                    result = ads.compute_adaptive_quantity(
                        100.0, 85.0, capital=500000.0, risk_pct=1.0, symbol="NIFTY",
                        regime=regime, alignment=alignment, institutional_backed=backed,
                    )
                    assert result.qty >= 0
