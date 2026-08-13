"""
test_agents/trading_intelligence/test_risk_filters.py -- Milestone 20,
Phase 6: regression tests for agents/trading_intelligence/risk_filters.py,
the advisory-only (non-executing) Smart Intraday Risk Filters.
"""
from agents.trading_intelligence import risk_filters as rf


def _ce_kwargs(**overrides):
    base = dict(state="BULLISH_RETEST_ACTIVE", confidence=90, spot=110.0, vwap=100.0,
                breakout_confirmed=True, premium_momentum_positive_2_candles=True)
    base.update(overrides)
    return base


def _pe_kwargs(**overrides):
    base = dict(state="BEARISH_RETEST_ACTIVE", confidence=90, spot=90.0, vwap=100.0,
                breakdown_confirmed=True, put_oi_increasing=True, call_oi_unwinding=True)
    base.update(overrides)
    return base


class TestEvaluateCE:
    def test_all_conditions_met_passes(self):
        result = rf.evaluate_ce(**_ce_kwargs())
        assert result.passed is True
        assert result.trade_quality is None
        assert result.reasons == []

    def test_wrong_state_is_rejected(self):
        result = rf.evaluate_ce(**_ce_kwargs(state="RANGE"))
        assert result.passed is False
        assert result.trade_quality == "FILTER_REJECTED"
        assert any("state" in r for r in result.reasons)

    def test_confidence_below_82_is_rejected(self):
        result = rf.evaluate_ce(**_ce_kwargs(confidence=81))
        assert result.passed is False
        assert any("confidence" in r for r in result.reasons)

    def test_confidence_exactly_82_passes(self):
        result = rf.evaluate_ce(**_ce_kwargs(confidence=82))
        assert result.passed is True

    def test_spot_not_above_vwap_is_rejected(self):
        result = rf.evaluate_ce(**_ce_kwargs(spot=99.0, vwap=100.0))
        assert result.passed is False
        assert any("VWAP" in r for r in result.reasons)

    def test_unconfirmed_breakout_is_rejected(self):
        result = rf.evaluate_ce(**_ce_kwargs(breakout_confirmed=False))
        assert result.passed is False

    def test_negative_premium_momentum_is_rejected(self):
        result = rf.evaluate_ce(**_ce_kwargs(premium_momentum_positive_2_candles=False))
        assert result.passed is False

    def test_multiple_failures_are_all_reported(self):
        result = rf.evaluate_ce(**_ce_kwargs(state="RANGE", confidence=10))
        assert len(result.reasons) >= 2

    def test_continuation_state_is_rejected_as_unsupported_not_silently_passed(self):
        # Final safety adjustment: BULLISH_CONTINUATION is in
        # CE_ALLOWED_STATES (the request that authorized this module
        # named it) but classify_market_state() has never actually
        # produced it -- KNOWN_MARKET_STATES membership is checked
        # FIRST, so this is rejected for the real reason
        # (unsupported_market_state), not silently allow-listed through.
        result = rf.evaluate_ce(**_ce_kwargs(state="BULLISH_CONTINUATION"))
        assert result.passed is False
        assert result.trade_quality == "FILTER_REJECTED"
        assert result.reasons == [rf.UNSUPPORTED_STATE_REASON]

    def test_unrecognized_state_string_is_rejected_as_unsupported(self):
        result = rf.evaluate_ce(**_ce_kwargs(state="SOMETHING_MADE_UP"))
        assert result.passed is False
        assert result.reasons == [rf.UNSUPPORTED_STATE_REASON]

    def test_none_state_is_rejected_as_unsupported(self):
        result = rf.evaluate_ce(**_ce_kwargs(state=None))
        assert result.passed is False
        assert result.reasons == [rf.UNSUPPORTED_STATE_REASON]

    def test_a_real_but_wrong_state_is_not_reported_as_unsupported(self):
        # RANGE is a genuinely real classify_market_state() output --
        # this must fail for "wrong state for CE", not get lumped in
        # with the unsupported-state short-circuit.
        result = rf.evaluate_ce(**_ce_kwargs(state="RANGE"))
        assert result.reasons != [rf.UNSUPPORTED_STATE_REASON]


class TestEvaluatePE:
    def test_all_conditions_met_passes(self):
        result = rf.evaluate_pe(**_pe_kwargs())
        assert result.passed is True
        assert result.trade_quality is None

    def test_wrong_state_is_rejected(self):
        result = rf.evaluate_pe(**_pe_kwargs(state="RANGE"))
        assert result.passed is False

    def test_confidence_below_82_is_rejected(self):
        result = rf.evaluate_pe(**_pe_kwargs(confidence=50))
        assert result.passed is False

    def test_spot_not_below_vwap_is_rejected(self):
        result = rf.evaluate_pe(**_pe_kwargs(spot=101.0, vwap=100.0))
        assert result.passed is False

    def test_put_oi_not_increasing_is_rejected(self):
        result = rf.evaluate_pe(**_pe_kwargs(put_oi_increasing=False))
        assert result.passed is False

    def test_call_oi_not_unwinding_is_rejected(self):
        result = rf.evaluate_pe(**_pe_kwargs(call_oi_unwinding=False))
        assert result.passed is False

    def test_continuation_state_is_rejected_as_unsupported_not_silently_passed(self):
        result = rf.evaluate_pe(**_pe_kwargs(state="BEARISH_CONTINUATION"))
        assert result.passed is False
        assert result.trade_quality == "FILTER_REJECTED"
        assert result.reasons == [rf.UNSUPPORTED_STATE_REASON]

    def test_unrecognized_state_string_is_rejected_as_unsupported(self):
        result = rf.evaluate_pe(**_pe_kwargs(state="SOMETHING_MADE_UP"))
        assert result.passed is False
        assert result.reasons == [rf.UNSUPPORTED_STATE_REASON]
