"""
test_mcx_bullion_strike_step.py -- regression test for the 2026-08-24
GOLD/GOLDM/SILVER/SILVERM strike-step fix.

Confirmed against the real, live Angel One instrument master
(exch_seg=="MCX" specifically -- see app.py's own comment above these
SYMBOLS entries): the nearest genuinely MCX-tradeable expiry's real listed
strikes are spaced 500 apart for GOLD/GOLDM and 1000 apart for
SILVER/SILVERM. The old step=100 for all four meant oi_engine.wanted_strikes()
generated mostly-phantom strikes -- only 1-2 of the usual 9 ever matched a
real contract, confirmed live via strikes.ce_trading_symbol being populated
on just 1-2 of 9 rows per cycle (verified: 2026-08-24, GOLD 1/9,
GOLDM 2/9, SILVER 1/9, SILVERM 1/9, vs. 9/9 for every other watched symbol).

SKIP_AUTOSTART=1 + import app, matching test_market_hours.py's own
established convention -- no DB needed, this is a pure config-value check.
"""
import os

os.environ["SKIP_AUTOSTART"] = "1"

import app


class TestMcxBullionStrikeStep:
    def test_gold_and_goldm_step_matches_real_mcx_spacing(self):
        assert app.SYMBOLS["GOLD"]["step"] == 500
        assert app.SYMBOLS["GOLDM"]["step"] == 500

    def test_silver_and_silverm_step_matches_real_mcx_spacing(self):
        assert app.SYMBOLS["SILVER"]["step"] == 1000
        assert app.SYMBOLS["SILVERM"]["step"] == 1000

    def test_crudeoil_and_naturalgas_families_left_unchanged(self):
        # Verified correct (CRUDEOIL/CRUDEOILM) or a harmless clean
        # multiple of the real spacing (NATURALGAS/NATGASMINI, real=5,
        # configured=10 -- every generated strike still exists, just at
        # coarser-than-maximum resolution) -- this fix must not touch them.
        assert app.SYMBOLS["CRUDEOIL"]["step"] == 50
        assert app.SYMBOLS["CRUDEOILM"]["step"] == 50
        assert app.SYMBOLS["NATURALGAS"]["step"] == 10
        assert app.SYMBOLS["NATGASMINI"]["step"] == 10

    def test_wanted_strikes_generated_at_the_new_step_are_all_multiples_of_it(self):
        from oi_engine import wanted_strikes

        for symbol, underlying in (("GOLD", 164400.0), ("SILVER", 258600.0)):
            step = app.SYMBOLS[symbol]["step"]
            strikes, atm = wanted_strikes(underlying, step, strikes_each_side=4)
            assert all(s % step == 0 for s in strikes)
            assert len(strikes) == 9
