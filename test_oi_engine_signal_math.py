"""Regression tests for oi_engine.generate_signal()'s core entry/SL/target
math -- the formula that decides every real (paper) trade's actual price
levels. Until now this was genuinely untested (see
test_oi_engine_momentum_confirmation.py's own docstring: "not a full
backfill of generate_signal()'s existing (untested) confidence logic --
that's a separate, larger task"). This is that task, scoped to entry/SL/
target specifically (not the confidence-scoring bonuses/penalties, which
stay out of scope here).

A companion real-data backtest (`python3 backtest.py --symbol <X> --from
2026-07-13 --to 2026-08-24`, i.e. simulate_trades() across every watched
symbol for the full available archive) was run alongside this file to
validate the SAME formula's live performance, not just its internal
consistency -- see the chat report for those numbers. These tests lock in
the *formula's own arithmetic*: given a fixed set of inputs, does it
compute what it's documented to compute, and do its own structural
invariants (SL below entry, target above entry) always hold. They do not
and cannot establish whether the strategy is profitable -- that's what the
backtest is for.
"""
import oi_engine
from oi_engine import StrikeRow, generate_signal


def _row(strike, ce_ltp=0.0, pe_ltp=0.0, ce_oi=0, pe_oi=0, ce_signal="Neutral", pe_signal="Neutral",
         ce_vol=0, pe_vol=0):
    return StrikeRow(
        strike=strike, ce_ltp=ce_ltp, pe_ltp=pe_ltp, ce_oi=ce_oi, pe_oi=pe_oi,
        ce_signal=ce_signal, pe_signal=pe_signal, ce_vol=ce_vol, pe_vol=pe_vol,
    )


# A simple, deterministic 5-strike chain around ATM=100 (step 50, matching
# generate_signal()'s own default strike_step) -- one clear OI wall on each
# side so wall selection isn't ambiguous in any test below.
def _chain(atm_ce_ltp=20.0, atm_pe_ltp=18.0):
    return [
        _row(0, pe_oi=500),                          # far support, low OI -- never picked over the real wall
        _row(50, pe_oi=9000),                         # the real support/invalidation wall
        _row(100, ce_ltp=atm_ce_ltp, pe_ltp=atm_pe_ltp),  # ATM
        _row(150, ce_oi=9000),                         # the real resistance/target wall
        _row(200, ce_oi=500),                          # far resistance, low OI
    ]


def _generate(bias="BULLISH BREAKOUT", **overrides):
    rows = overrides.pop("rows", _chain())
    support, resistance = oi_engine.oi_walls(rows)
    kwargs = dict(
        rows=rows, atm=100, bias=bias, note="test", pcr=1.0,
        support=support, resistance=resistance,
    )
    kwargs.update(overrides)
    return generate_signal(**kwargs)


class TestNoTradeGuards:
    """generate_signal() must degrade to NO_TRADE, never raise or fabricate
    a price, whenever it genuinely doesn't have enough to trade on."""

    def test_no_atm_row_is_no_trade(self):
        sig = _generate(rows=[_row(50), _row(150)])
        assert sig["action"] == "NO_TRADE"
        assert "ATM" in sig["reason"]

    def test_neutral_bias_is_no_trade(self):
        sig = _generate(bias="NEUTRAL")
        assert sig["action"] == "NO_TRADE"

    def test_range_bias_is_no_trade(self):
        sig = _generate(bias="RANGE")
        assert sig["action"] == "NO_TRADE"

    def test_zero_ltp_is_no_trade(self):
        sig = _generate(bias="BULLISH BREAKOUT", rows=_chain(atm_ce_ltp=0.0))
        assert sig["action"] == "NO_TRADE"
        assert "No valid LTP" in sig["reason"]

    def test_negative_ltp_is_no_trade(self):
        sig = _generate(bias="BULLISH BREAKOUT", rows=_chain(atm_ce_ltp=-5.0))
        assert sig["action"] == "NO_TRADE"


class TestActionIsAlwaysBuy:
    """ai_trading_engine.py structurally requires this (asserts action is
    one of exactly "BUY CE"/"BUY PE") -- this engine never recommends a
    short/sell. Locking that contract in here too, at the source."""

    def test_bullish_bias_is_buy_ce(self):
        sig = _generate(bias="BULLISH BREAKOUT")
        assert sig["action"] == "BUY CE"
        assert sig["direction"] == "CE"

    def test_bearish_bias_is_buy_pe(self):
        sig = _generate(bias="BEARISH BREAKDOWN")
        assert sig["action"] == "BUY PE"
        assert sig["direction"] == "PE"

    def test_reversal_up_risk_is_buy_ce(self):
        assert _generate(bias="REVERSAL UP RISK")["action"] == "BUY CE"

    def test_reversal_down_risk_is_buy_pe(self):
        assert _generate(bias="REVERSAL DOWN RISK")["action"] == "BUY PE"


class TestEntryPrice:
    def test_entry_is_the_atm_rows_own_ce_ltp_for_a_ce_signal(self):
        sig = _generate(bias="BULLISH BREAKOUT", rows=_chain(atm_ce_ltp=23.45))
        assert sig["entry_price"] == 23.45

    def test_entry_is_the_atm_rows_own_pe_ltp_for_a_pe_signal(self):
        sig = _generate(bias="BEARISH BREAKDOWN", rows=_chain(atm_pe_ltp=17.65))
        assert sig["entry_price"] == 17.65


class TestTargetFormula:
    """target_price = entry + max(delta_used * |wall_strike - atm|, entry * min_target_percent)
    -- same formula regardless of CE/PE (it operates on premium, which
    always increases toward profit in either direction), using the
    strongest OI wall on the PROFIT side (resistance for CE, support for
    PE)."""

    def test_delta_projection_wins_when_it_exceeds_the_percent_floor(self):
        # wall is 50 points away (150 - 100), default delta 0.55 ->
        # projected_move = 27.5, vs the 15%-of-entry floor = 3.0 (entry=20).
        # Delta projection should win.
        sig = _generate(bias="BULLISH BREAKOUT", rows=_chain(atm_ce_ltp=20.0))
        entry = sig["entry_price"]
        expected_projected = abs(150 - 100) * 0.55
        expected_floor = entry * 0.15
        assert expected_projected > expected_floor
        assert sig["target_price"] == round(entry + expected_projected, 2)

    def test_percent_floor_wins_when_delta_projection_is_smaller(self):
        # Force a tiny delta so the projected move collapses well below the
        # 15%-of-entry floor.
        sig = _generate(bias="BULLISH BREAKOUT", rows=_chain(atm_ce_ltp=20.0), target_delta_approx=0.01)
        entry = sig["entry_price"]
        expected_projected = abs(150 - 100) * 0.01   # 0.5
        expected_floor = entry * 0.15                 # 3.0
        assert expected_floor > expected_projected
        assert sig["target_price"] == round(entry + expected_floor, 2)

    def test_target_is_always_above_entry(self):
        for delta in (0.01, 0.3, 0.55, 0.9):
            sig = _generate(bias="BULLISH BREAKOUT", target_delta_approx=delta)
            assert sig["target_price"] > sig["entry_price"]

    def test_target_points_matches_target_minus_entry(self):
        sig = _generate(bias="BULLISH BREAKOUT")
        assert sig["target_points"] == round(sig["target_price"] - sig["entry_price"], 2)

    def test_pe_target_uses_the_support_side_wall(self):
        # For a PE signal the profit-side wall is `support` (150 has no PE
        # OI in this fixture; 50 is the real PE wall) -- rebuild a chain
        # where the PE wall sits somewhere distinct so this is unambiguous.
        rows = [
            _row(0), _row(50, pe_oi=9000), _row(100, ce_ltp=20.0, pe_ltp=18.0),
            _row(150, ce_oi=9000), _row(200),
        ]
        sig = _generate(bias="BEARISH BREAKDOWN", rows=rows)
        expected_projected = abs(50 - 100) * 0.55
        expected_floor = sig["entry_price"] * 0.15
        assert sig["target_price"] == round(sig["entry_price"] + max(expected_projected, expected_floor), 2)


class TestStopLossFormula:
    """sl_price = max(structural_invalidation_sl, percent_floor_sl, 0.05) --
    the TIGHTER (higher/closer-to-entry) of a structural stop (projected
    from the nearest OPPOSING wall) and a flat percent-of-entry floor,
    with a hard 5-paise minimum. Structural invalidation picks the
    NEAREST correct-side wall by distance, not the highest-OI one (a
    documented past bug fix in oi_engine.py -- iterating OI-ranked walls
    and taking the first match could pick a wall several strikes further
    away than the true nearest one)."""

    def test_sl_is_always_below_entry(self):
        for delta in (0.01, 0.3, 0.55, 0.9):
            sig = _generate(bias="BULLISH BREAKOUT", target_delta_approx=delta)
            assert sig["sl_price"] < sig["entry_price"]

    def test_sl_points_matches_entry_minus_sl(self):
        sig = _generate(bias="BULLISH BREAKOUT")
        assert sig["sl_points"] == round(sig["entry_price"] - sig["sl_price"], 2)

    def test_ce_invalidation_strike_is_the_nearest_support_below_atm(self):
        # Two support candidates below ATM=100: 50 (near) and 0 (far).
        # oi_walls() ranks by OI (0 has more OI here), but invalidation
        # selection must still pick 50 -- the nearer one -- not 0.
        rows = [
            _row(0, pe_oi=20000),   # far, but highest OI -- must NOT be picked
            _row(50, pe_oi=100),    # near, lower OI -- must be picked
            _row(100, ce_ltp=20.0, pe_ltp=18.0),
            _row(150, ce_oi=9000), _row(200),
        ]
        sig = _generate(bias="BULLISH BREAKOUT", rows=rows)
        assert sig["sl_invalidation_strike"] == 50

    def test_pe_invalidation_strike_is_the_nearest_resistance_above_atm(self):
        rows = [
            _row(0), _row(50, pe_oi=9000),
            _row(100, ce_ltp=20.0, pe_ltp=18.0),
            _row(150, ce_oi=100),    # near, lower OI -- must be picked
            _row(200, ce_oi=20000),  # far, but highest OI -- must NOT be picked
        ]
        sig = _generate(bias="BEARISH BREAKDOWN", rows=rows)
        assert sig["sl_invalidation_strike"] == 150

    def test_percent_floor_engages_when_structural_sl_would_be_wider(self):
        # A very large delta blows the structural stop distance far past
        # the 35%-of-entry floor -- the floor (tighter) must win.
        sig = _generate(bias="BULLISH BREAKOUT", rows=_chain(atm_ce_ltp=20.0), target_delta_approx=5.0)
        entry = sig["entry_price"]
        floor_sl = round(entry * (1 - 0.35), 2)
        assert sig["sl_price"] == floor_sl

    def test_no_opposing_wall_falls_back_to_one_strike_step(self):
        # Every strike has zero PE OI -- oi_walls() still returns rows (its
        # top-3 by value, all tied at 0), but none of them sit strictly
        # below ATM on the correct side after the candidate filter, so
        # generate_signal() must fall back to atm - strike_step.
        rows = [_row(100, ce_ltp=20.0, pe_ltp=18.0)]
        sig = _generate(bias="BULLISH BREAKOUT", rows=rows, strike_step=50)
        assert sig["sl_invalidation_strike"] == 50   # 100 - 50

    def test_sl_never_drops_below_five_paise_floor(self):
        # A tiny entry price with an aggressive delta could otherwise drive
        # the structural SL to zero or negative.
        sig = _generate(bias="BULLISH BREAKOUT", rows=_chain(atm_ce_ltp=0.10), target_delta_approx=5.0)
        assert sig["sl_price"] >= 0.05


class TestDeltaSource:
    def test_defaults_to_flat_approximation_without_nse_data(self):
        sig = _generate(bias="BULLISH BREAKOUT")
        assert sig["delta_used"] == 0.55
        assert "flat approximation" in sig["delta_source"]

    def test_custom_target_delta_approx_is_honored(self):
        sig = _generate(bias="BULLISH BREAKOUT", target_delta_approx=0.4)
        assert sig["delta_used"] == 0.4


class TestConfidenceGate:
    def test_tradeable_flag_matches_confidence_threshold(self):
        sig = _generate(bias="BULLISH BREAKOUT", confidence_threshold=200)
        assert sig["tradeable"] is False
        sig2 = _generate(bias="BULLISH BREAKOUT", confidence_threshold=0)
        assert sig2["tradeable"] is True
