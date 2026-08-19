"""Regression tests for oi_engine.generate_signal()'s new, flag-gated
momentum-confirmation sub-score (reuses agents.quant_researcher.features.
momentum_exhaustion() against the same recent-candle slice
build_market_structure() already computes -- see that parameter's own
docstring in oi_engine.py). Scoped to this one new behavior, not a full
backfill of generate_signal()'s existing (untested) confidence logic --
that's a separate, larger task."""
import oi_engine
from oi_engine import StrikeRow, generate_signal


def _atm_row(strike=100, ce_ltp=10.0, pe_ltp=10.0):
    return StrikeRow(strike=strike, ce_ltp=ce_ltp, pe_ltp=pe_ltp)


def _base_kwargs(**overrides):
    atm_row = _atm_row()
    kwargs = dict(
        rows=[atm_row], atm=100, bias="BULLISH BREAKOUT", note="test",
        pcr=1.0, support=[], resistance=[],
    )
    kwargs.update(overrides)
    return kwargs


def _rising_closes(n=30, start=100.0, step=1.0):
    """Rises hard to push RSI into overbought (>70), then ticks DOWN for
    the last two bars -- momentum_exhaustion()'s overbought condition
    needs RSI already rolling over (roc<0), not merely high, so a purely
    monotonic rise never triggers it. A CE (bullish) entry right after
    this shape is entering INTO exhaustion."""
    closes = [start + i * step for i in range(n - 2)]
    closes += [closes[-1] - 0.5, closes[-1] - 1.0]  # small pullback at the end
    return [{"close": c, "high": c + 0.5, "low": c - 0.5, "open": c} for c in closes]


def _falling_then_bounce_closes(n=30):
    """Falls hard, then ticks up at the very end -- the oversold-with-
    positive-RSI-roc shape momentum_exhaustion() looks for."""
    closes = [100.0 - i * 1.0 for i in range(n - 2)]
    closes += [closes[-1] + 0.5, closes[-1] + 1.0]  # small bounce at the end
    return [{"close": c, "high": c + 0.5, "low": c - 0.5, "open": c} for c in closes]


def _flat_closes(n=30, price=100.0):
    return [{"close": price, "high": price + 0.1, "low": price - 0.1, "open": price} for _ in range(n)]


class TestFlagOff:
    def test_default_is_off_and_confidence_unaffected(self):
        """Deploying this change must not alter existing behavior when the
        flag isn't explicitly passed True -- the core safety requirement."""
        sig_without = generate_signal(**_base_kwargs())
        sig_with_candles_but_flag_off = generate_signal(
            **_base_kwargs(candles=_rising_closes(), momentum_confirmation_enabled=False)
        )
        assert sig_without["confidence"] == sig_with_candles_but_flag_off["confidence"]

    def test_no_candles_is_a_silent_noop_even_with_flag_on(self):
        sig = generate_signal(**_base_kwargs(candles=None, momentum_confirmation_enabled=True))
        baseline = generate_signal(**_base_kwargs())
        assert sig["confidence"] == baseline["confidence"]


class TestMomentumAdjustsConfidence:
    def test_ce_entering_overbought_exhaustion_is_penalized(self):
        baseline = generate_signal(**_base_kwargs())["confidence"]
        sig = generate_signal(**_base_kwargs(
            bias="BULLISH BREAKOUT", candles=_rising_closes(),
            momentum_confirmation_enabled=True, momentum_penalty=10,
        ))
        assert sig["confidence"] == baseline - 10
        assert "contradicts" in sig["reason"]

    def test_ce_confirmed_by_oversold_bounce_is_boosted(self):
        baseline = generate_signal(**_base_kwargs())["confidence"]
        sig = generate_signal(**_base_kwargs(
            bias="BULLISH BREAKOUT", candles=_falling_then_bounce_closes(),
            momentum_confirmation_enabled=True, momentum_bonus=10,
        ))
        assert sig["confidence"] == baseline + 10
        assert "confirms" in sig["reason"]

    def test_pe_direction_flips_the_same_reading(self):
        """The same rising-price series that PENALIZES a CE entry (buying
        into overbought) must CONFIRM a PE entry (shorting overbought) --
        proves the direction-flip logic, not just that some adjustment fires."""
        baseline = generate_signal(**_base_kwargs(bias="BEARISH BREAKDOWN"))["confidence"]
        sig = generate_signal(**_base_kwargs(
            bias="BEARISH BREAKDOWN", candles=_rising_closes(),
            momentum_confirmation_enabled=True, momentum_bonus=10,
        ))
        assert sig["confidence"] == baseline + 10
        assert "confirms" in sig["reason"]

    def test_flat_market_has_no_extreme_and_leaves_confidence_unchanged(self):
        baseline = generate_signal(**_base_kwargs())["confidence"]
        sig = generate_signal(**_base_kwargs(
            candles=_flat_closes(), momentum_confirmation_enabled=True,
        ))
        assert sig["confidence"] == baseline
        assert "momentum" not in sig["reason"].lower()


class TestConfidenceStillClamped:
    def test_bonus_never_pushes_confidence_above_95(self):
        # stack every other positive bump too (dual-source/order-flow/etc are
        # skipped here since nse_atm_row=None) -- just confirm the ceiling holds
        sig = generate_signal(**_base_kwargs(
            pcr=1.5, bias="BULLISH BREAKOUT",
            candles=_falling_then_bounce_closes(), momentum_confirmation_enabled=True,
            momentum_bonus=1000,
        ))
        assert sig["confidence"] <= 95

    def test_penalty_never_pushes_confidence_below_20(self):
        sig = generate_signal(**_base_kwargs(
            candles=_rising_closes(), momentum_confirmation_enabled=True,
            momentum_penalty=1000,
        ))
        assert sig["confidence"] >= 20


class TestFailureIsolation:
    def test_malformed_candles_never_raises_and_confidence_falls_back(self):
        """A momentum-feature failure must never break signal generation --
        confirms the try/except actually catches something real, not just
        that it's syntactically present."""
        baseline = generate_signal(**_base_kwargs())["confidence"]
        malformed = [{"no_close_key_here": 1.0}] * 20
        sig = generate_signal(**_base_kwargs(
            candles=malformed, momentum_confirmation_enabled=True,
        ))
        assert sig["confidence"] == baseline  # no crash, no fabricated adjustment
        assert sig["action"].startswith("BUY")  # a real trade signal came back, not a NO_TRADE dict

    def test_too_few_candles_is_skipped_not_crashed(self):
        baseline = generate_signal(**_base_kwargs())["confidence"]
        sig = generate_signal(**_base_kwargs(
            candles=_rising_closes(n=5), momentum_confirmation_enabled=True,
        ))
        assert sig["confidence"] == baseline
