from agents.trading_intelligence import regime_profile as rp
from test_agents.trading_intelligence.conftest import (
    insert_cycle,
    insert_market_structure,
    insert_realistic_chain,
    insert_strike,
)


class TestVolatilityRegime:
    def test_unknown_below_minimum_history(self, ti_db):
        regime, pct = rp._volatility_regime(15.0, [10.0, 11.0])  # only 2 readings, need 5
        assert regime == "UNKNOWN"
        assert pct is None

    def test_unknown_when_current_atr_missing(self, ti_db):
        regime, pct = rp._volatility_regime(None, [10.0, 11.0, 12.0, 13.0, 14.0])
        assert regime == "UNKNOWN"
        assert pct is None

    def test_high_when_current_is_the_top_of_its_own_range(self, ti_db):
        regime, pct = rp._volatility_regime(30.0, [10.0, 12.0, 14.0, 16.0, 18.0])
        assert regime == "HIGH"
        assert pct == 100.0

    def test_low_when_current_is_the_bottom_of_its_own_range(self, ti_db):
        regime, pct = rp._volatility_regime(5.0, [10.0, 12.0, 14.0, 16.0, 18.0])
        assert regime == "LOW"
        assert pct == 0.0

    def test_normal_when_current_is_mid_range(self, ti_db):
        regime, pct = rp._volatility_regime(14.0, [10.0, 12.0, 14.0, 16.0, 18.0])
        assert regime == "NORMAL"

    def test_normal_when_history_is_perfectly_flat(self, ti_db):
        regime, pct = rp._volatility_regime(10.0, [10.0, 10.0, 10.0, 10.0, 10.0])
        assert regime == "NORMAL"
        assert pct == 50.0

    def test_none_values_and_zeros_in_history_are_ignored(self, ti_db):
        # 5 usable readings required -- Nones/zeros must not count toward that.
        regime, pct = rp._volatility_regime(30.0, [10.0, None, 0.0, 12.0, 14.0, 16.0, 18.0])
        assert regime == "HIGH"


class TestPersistence:
    def test_no_history_is_not_persistent(self, ti_db):
        persistent, cycles = rp._persistence([], "ce_signal")
        assert persistent is False
        assert cycles == 0

    def test_neutral_current_signal_is_never_persistent(self, ti_db):
        history = [{"ce_signal": "Neutral"}, {"ce_signal": "Neutral"}, {"ce_signal": "Neutral"}]
        persistent, cycles = rp._persistence(history, "ce_signal")
        assert persistent is False
        assert cycles == 0

    def test_persistent_when_min_cycles_agree_consecutively(self, ti_db):
        history = [{"ce_signal": "Long Buildup"}] * 4
        persistent, cycles = rp._persistence(history, "ce_signal")
        assert persistent is True
        assert cycles == 4

    def test_not_yet_persistent_below_min_cycles(self, ti_db):
        history = [{"ce_signal": "Long Buildup"}, {"ce_signal": "Long Buildup"}, {"ce_signal": "Neutral"}]
        persistent, cycles = rp._persistence(history, "ce_signal")
        assert persistent is False
        assert cycles == 2

    def test_stops_counting_at_first_disagreement(self, ti_db):
        history = [
            {"ce_signal": "Long Buildup"}, {"ce_signal": "Long Buildup"}, {"ce_signal": "Long Buildup"},
            {"ce_signal": "Short Buildup"}, {"ce_signal": "Long Buildup"},  # older, disagreeing/agreeing -- irrelevant
        ]
        persistent, cycles = rp._persistence(history, "ce_signal")
        assert persistent is True
        assert cycles == 3


class TestClassify:
    def test_unavailable_symbol_degrades_honestly(self, ti_db):
        profile = rp.classify("NOT_A_REAL_SYMBOL")
        assert profile.trend_regime == "UNKNOWN"
        assert profile.adx is None
        assert profile.volatility_regime == "UNKNOWN"
        assert profile.atm_strike is None
        assert profile.ce_buildup_persistent is False
        assert profile.pe_buildup_persistent is False

    def test_trend_regime_reflects_stored_adx(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        insert_market_structure(ti_db, symbol="NIFTY", ts="2026-08-06T10:00:00", adx=30.0)
        profile = rp.classify("NIFTY")
        assert profile.trend_regime == "TRENDING"
        assert profile.adx == 30.0

    def test_volatility_regime_computed_from_real_history(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        # 5 prior readings (need >= VOLATILITY_RANK_MIN_HISTORY=5) + 1 current outlier-high reading.
        for i, atr in enumerate((10.0, 11.0, 12.0, 14.0, 16.0, 30.0)):
            insert_market_structure(ti_db, symbol="NIFTY", ts=f"2026-08-06T09:{15 + i}:00", atr_14=atr)
        profile = rp.classify("NIFTY")
        assert profile.volatility_regime == "HIGH"

    def test_atm_persistence_reflects_real_strike_history(self, ti_db):
        cid, strikes = insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        # insert_realistic_chain's own cycle is the LATEST (current) one -- its
        # ATM strike defaults to ce_signal="Neutral"; override it, then add
        # 3 MORE, older cycles with the same non-neutral signal before it.
        import sqlite3
        conn = sqlite3.connect(ti_db)
        conn.execute("UPDATE strikes SET ce_signal='Long Buildup' WHERE cycle_id=? AND strike=24500", (cid,))
        conn.commit()
        conn.close()
        for i in range(3):
            older_cid = insert_cycle(ti_db, symbol="NIFTY", ts=f"2026-08-06T09:{10 + i}:00",
                                      underlying_ltp=24505.0, atm=24500.0)
            insert_strike(ti_db, older_cid, 24500, ce_signal="Long Buildup")

        profile = rp.classify("NIFTY")
        assert profile.atm_strike == 24500
        assert profile.ce_buildup_persistent is True
        assert profile.ce_persistence_cycles == 4  # the current cycle + 3 older ones

    def test_snapshot_and_market_structure_can_be_prefetched(self, ti_db):
        """Dedup discipline: a caller that already has both (the way
        ai_trading_engine.evaluate() will, once wired in) must be able to
        pass them straight through without a second read."""
        from agents.trading_intelligence import data_access, market_data

        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505, atm=24500)
        insert_market_structure(ti_db, symbol="NIFTY", ts="2026-08-06T10:00:00", adx=10.0)
        snapshot = market_data.get_snapshot("NIFTY")
        market_structure = data_access.latest_market_structure("NIFTY")

        profile = rp.classify("NIFTY", snapshot=snapshot, market_structure=market_structure)
        assert profile.trend_regime == "RANGING"


class TestReplay:
    """Milestone 11 Plan's own success criterion for this module: 'computed
    for every symbol every cycle with zero exceptions across a 20+ cycle
    replay test' -- the same shape M10's own test_validation.py::TestReplay
    already established."""

    def test_25_cycle_replay_never_raises_and_stays_internally_consistent(self, ti_db):
        underlying = 24500.0
        for i in range(25):
            underlying += (4 if i % 2 == 0 else -3)
            insert_realistic_chain(
                ti_db, symbol="NIFTY", underlying_ltp=underlying, atm=round(underlying / 50) * 50,
                ts=f"2026-08-06T09:{15 + i}:00",
            )
            insert_market_structure(
                ti_db, symbol="NIFTY", ts=f"2026-08-06T09:{15 + i}:00",
                adx=10.0 + (i % 20), atr_14=8.0 + (i % 7),
            )
            profile = rp.classify("NIFTY")
            assert profile.trend_regime in ("TRENDING", "RANGING", "TRANSITIONING", "UNKNOWN")
            assert profile.volatility_regime in ("HIGH", "NORMAL", "LOW", "UNKNOWN")
            if profile.volatility_regime == "UNKNOWN":
                assert profile.volatility_percentile is None
            else:
                assert 0.0 <= profile.volatility_percentile <= 100.0
            assert profile.ce_persistence_cycles >= 0
            assert profile.pe_persistence_cycles >= 0
