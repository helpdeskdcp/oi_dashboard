"""
test_position_sizing.py -- regression tests for position_sizing.py (Exit
Engine V4 Phase 1: Dynamic Position Sizing). Pure-function module, no DB/
network dependency.
"""
import pytest

from position_sizing import compute_quantity


class TestBackwardCompatibility:
    def test_no_sizing_mode_always_returns_one(self):
        # The critical backward-compat guarantee: every existing caller
        # that never passes sizing_mode must keep getting quantity=1, so
        # backtest.py's pnl_amount = points * quantity stays == points.
        assert compute_quantity(entry=100.0, initial_sl=90.0) == 1
        assert compute_quantity(entry=100.0, initial_sl=90.0, sizing_mode=None,
                                 capital=100000, risk_pct=1.0) == 1


class TestFixedMode:
    def test_fixed_qty_used_as_is(self):
        assert compute_quantity(100.0, 90.0, sizing_mode="fixed", fixed_qty=25) == 25

    def test_fixed_without_fixed_qty_falls_back_to_one(self):
        assert compute_quantity(100.0, 90.0, sizing_mode="fixed") == 1


class TestRiskPctMode:
    def test_spec_example_capital_100000_risk_1pct_sl_50pts(self):
        # From the spec: capital=100000, risk=1% -> max loss 1000; a trade
        # risking 50pts/unit should size to 1000/50 = 20.
        qty = compute_quantity(entry=24100.0, initial_sl=24050.0, sizing_mode="risk_pct",
                                capital=100000, risk_pct=1.0)
        assert qty == 20

    def test_direction_agnostic_uses_absolute_distance(self):
        # SELL-side trade: initial_sl ABOVE entry -- same |entry-sl| distance.
        qty = compute_quantity(entry=24050.0, initial_sl=24100.0, sizing_mode="risk_pct",
                                capital=100000, risk_pct=1.0)
        assert qty == 20

    def test_wider_stop_produces_smaller_quantity(self):
        tight = compute_quantity(100.0, 95.0, sizing_mode="risk_pct", capital=100000, risk_pct=1.0)
        wide = compute_quantity(100.0, 50.0, sizing_mode="risk_pct", capital=100000, risk_pct=1.0)
        assert wide < tight   # same risk budget, wider stop -> fewer units -- the whole point of risk-based sizing

    def test_floors_to_whole_units_never_overshoots_risk_budget(self):
        # risk_amount=1000, per_unit_risk=33 -> 30.30... units would risk
        # slightly MORE than budgeted if rounded up. Must floor.
        qty = compute_quantity(100.0, 67.0, sizing_mode="risk_pct", capital=100000, risk_pct=1.0)
        assert qty == 30
        assert qty * 33 <= 1000

    def test_missing_capital_or_risk_pct_falls_back_to_fixed_qty_or_one(self):
        assert compute_quantity(100.0, 90.0, sizing_mode="risk_pct", risk_pct=1.0) == 1   # no capital
        assert compute_quantity(100.0, 90.0, sizing_mode="risk_pct", capital=100000) == 1   # no risk_pct
        assert compute_quantity(100.0, 90.0, sizing_mode="risk_pct", fixed_qty=7) == 7   # explicit fallback

    def test_degenerate_zero_distance_stop_falls_back_never_divides_by_zero(self):
        assert compute_quantity(100.0, 100.0, sizing_mode="risk_pct", capital=100000, risk_pct=1.0) == 1

    def test_risk_budget_too_small_for_stop_distance_can_size_to_zero(self):
        # Institutional risk sizing must be allowed to say "skip this trade,
        # your stop is too wide for the risk you're willing to take" --
        # forcing a minimum here would silently blow the stated risk budget.
        qty = compute_quantity(100.0, 0.0, sizing_mode="risk_pct", capital=1000, risk_pct=1.0)   # risk 10, stop 100pts away
        assert qty == 0


class TestQuantityClamps:
    def test_min_qty_raises_a_too_small_result(self):
        qty = compute_quantity(100.0, 99.0, sizing_mode="risk_pct", capital=1000, risk_pct=0.1,
                                min_qty=5)
        assert qty == 5

    def test_max_qty_caps_a_too_large_result(self):
        qty = compute_quantity(100.0, 99.0, sizing_mode="risk_pct", capital=10_000_000, risk_pct=1.0,
                                max_qty=100)
        assert qty == 100

    def test_clamps_apply_in_fixed_mode_too(self):
        assert compute_quantity(100.0, 90.0, sizing_mode="fixed", fixed_qty=500, max_qty=50) == 50
        assert compute_quantity(100.0, 90.0, sizing_mode="fixed", fixed_qty=1, min_qty=10) == 10

    def test_never_returns_negative(self):
        assert compute_quantity(100.0, 90.0, sizing_mode="fixed", fixed_qty=5, max_qty=-1) == 0


class TestInvalidMode:
    def test_unknown_sizing_mode_raises(self):
        with pytest.raises(ValueError):
            compute_quantity(100.0, 90.0, sizing_mode="not-a-real-mode")
