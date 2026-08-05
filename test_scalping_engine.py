"""
test_scalping_engine.py -- regression tests for scalping_engine.py (new
dedicated fast-timeframe scalping signal engine).

Synthetic data only -- no live broker/market dependency, same philosophy as
test_engine.py.
"""
import datetime as dt

from oi_engine import StrikeRow
from scalping_engine import (
    volatility_regime_multiplier, compute_scalp_targets_sl,
    evaluate_scalp_candidate, generate_scalp_signal,
)


def _row(strike=25000, ce_ltp=100.0, pe_ltp=90.0, ce_vol=5000, pe_vol=4000,
         ce_oi_chg=1000, pe_oi_chg=-200, ce_iv=15.0, pe_iv=15.0):
    return StrikeRow(strike=strike, ce_ltp=ce_ltp, pe_ltp=pe_ltp, ce_vol=ce_vol, pe_vol=pe_vol,
                      ce_oi_chg=ce_oi_chg, pe_oi_chg=pe_oi_chg, ce_iv=ce_iv, pe_iv=pe_iv)


class TestVolatilityRegimeMultiplier:
    def test_trending_scales_up(self):
        assert volatility_regime_multiplier({"regime": "TRENDING", "adx": 40}) > 1.0

    def test_ranging_scales_down(self):
        assert volatility_regime_multiplier({"regime": "RANGING", "adx": 10}) < 1.0

    def test_missing_data_is_neutral(self):
        assert volatility_regime_multiplier(None) == 1.0
        assert volatility_regime_multiplier({}) == 1.0

    def test_always_clamped(self):
        # Extreme regime + ADX must never blow past the sane band.
        assert volatility_regime_multiplier({"regime": "TRENDING", "adx": 999}) <= 1.6
        assert volatility_regime_multiplier({"regime": "RANGING", "adx": -999}) >= 0.6


class TestScalpTargetsSl:
    def test_sl_never_exceeds_max_pct_floor(self):
        # Huge ATR must still be capped by max_sl_pct, not blow out the stop.
        target, sl = compute_scalp_targets_sl(entry_price=100, underlying=25000, atr=5000,
                                               delta_used=0.6, regime_mult=1.6, max_sl_pct=0.035)
        assert sl >= 100 * (1 - 0.035) - 0.01

    def test_target_at_least_min_pct(self):
        # Tiny/no ATR must still clamp to the min_target_pct floor.
        target, sl = compute_scalp_targets_sl(entry_price=100, underlying=25000, atr=0,
                                               delta_used=0.5, regime_mult=1.0, min_target_pct=0.06)
        assert target >= 100 * 1.06 - 0.01

    def test_sl_never_below_5_paise(self):
        target, sl = compute_scalp_targets_sl(entry_price=0.10, underlying=25000, atr=50,
                                               delta_used=0.9, regime_mult=1.6)
        assert sl >= 0.05


class TestEvaluateScalpCandidate:
    def test_no_premium_returns_not_tradeable(self):
        row = _row(ce_ltp=0.0)
        result = evaluate_scalp_candidate(row, "CE", 25000, {}, [], [], "26AUG2026")
        assert result["tradeable"] is False
        assert "premium" in result["reason"].lower()

    def test_insufficient_history_returns_not_tradeable(self):
        row = _row()
        result = evaluate_scalp_candidate(row, "CE", 25000, {}, [{"ltp": 95}], [], "26AUG2026")
        assert result["tradeable"] is False

    def test_full_confirmation_produces_tradeable_signal(self):
        # A clean rising-premium history (entry trigger + EMA momentum both
        # satisfied), rising volume (expansion confirmed), bullish OI, and
        # underlying above VWAP -- every gate should pass.
        row = _row(ce_ltp=112.0, ce_vol=9000, ce_oi_chg=1500)
        premium_history = [{"ltp": v, "time": f"t{i}"} for i, v in
                            enumerate([90, 92, 94, 96, 98, 100, 103, 106, 109])]
        volume_history = [1000, 1100, 1050, 1200, 1300, 1250, 1400]
        market_structure = {"vwap": 24900, "atr_14": 60, "regime": "TRENDING", "adx": 30}
        result = evaluate_scalp_candidate(
            row, "CE", underlying=25000, market_structure=market_structure,
            premium_history=premium_history, volume_history=volume_history,
            expiry_date=dt.date(2026, 8, 26), now=dt.datetime(2026, 7, 31, 10, 0),
        )
        assert result["tradeable"] is True
        assert result["target_price"] > result["entry_price"] > result["sl_price"]
        assert result["max_hold_minutes"] == 6
        assert result["risk_reward"] >= 1.2

    def test_weak_volume_blocks_entry(self):
        # Premium broke its trigger, but volume never expanded -- fake-breakout
        # filter must block it.
        row = _row(ce_ltp=112.0, ce_vol=1000, ce_oi_chg=1500)
        premium_history = [{"ltp": v, "time": f"t{i}"} for i, v in
                            enumerate([90, 92, 94, 96, 98, 100, 103, 106, 109])]
        volume_history = [1000, 1050, 990, 1010, 1000, 1020, 1000]   # flat, no expansion
        market_structure = {"vwap": 24900, "atr_14": 60, "regime": "TRENDING", "adx": 30}
        result = evaluate_scalp_candidate(
            row, "CE", underlying=25000, market_structure=market_structure,
            premium_history=premium_history, volume_history=volume_history,
            expiry_date="26AUG2026",
        )
        assert result["tradeable"] is False
        assert "filter" in result["reason"].lower()

    def test_never_raises_on_missing_market_structure(self):
        row = _row(ce_ltp=112.0)
        premium_history = [{"ltp": v} for v in [90, 92, 94, 96, 98, 100, 103, 106, 109]]
        result = evaluate_scalp_candidate(row, "CE", 25000, None, premium_history, [1000, 1200, 1500],
                                           expiry_date=None)
        assert "tradeable" in result   # must return a dict, never raise


class TestGenerateScalpSignal:
    def test_returns_both_directions(self):
        rows = [_row(strike=24950), _row(strike=25000), _row(strike=25050)]
        result = generate_scalp_signal(
            rows, atm=25000, strike_step=50, underlying=25000, market_structure={},
            premium_history_by_key={}, volume_history_by_key={}, expiry_date="26AUG2026",
        )
        assert set(result.keys()) == {"CE", "PE"}
        assert result["CE"]["tradeable"] is False   # no history yet -- must degrade, not raise
        assert result["PE"]["tradeable"] is False

    def test_no_rows_never_raises(self):
        result = generate_scalp_signal([], atm=25000, strike_step=50, underlying=25000,
                                        market_structure={}, premium_history_by_key={},
                                        volume_history_by_key={}, expiry_date="26AUG2026")
        assert result["CE"]["tradeable"] is False
        assert result["PE"]["tradeable"] is False
