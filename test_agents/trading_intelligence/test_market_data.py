import datetime as dt

from agents.trading_intelligence import market_data as md
from test_agents.trading_intelligence.conftest import insert_realistic_chain


class TestGetSnapshot:
    def test_unavailable_when_never_logged(self, ti_db):
        snap = md.get_snapshot("NOT_A_REAL_SYMBOL")
        assert snap.available is False
        assert "no option-chain cycle" in snap.reason

    def test_available_snapshot_has_real_aggregates(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY")
        snap = md.get_snapshot("NIFTY", expiry_date=dt.date.today() + dt.timedelta(days=2))
        assert snap.available is True
        assert snap.total_ce_oi == 50000 * 9
        assert snap.total_pe_oi == 60000 * 9
        assert len(snap.strikes) == 9

    def test_pcr_change_computed_from_two_most_recent_cycles(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", pcr=1.0, ts="2026-08-06T09:15:00")
        insert_realistic_chain(ti_db, symbol="NIFTY", pcr=1.15, ts="2026-08-06T09:18:00")
        snap = md.get_snapshot("NIFTY")
        assert snap.pcr == 1.15
        assert snap.pcr_change == 0.15

    def test_pcr_change_is_none_with_only_one_cycle(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY")
        snap = md.get_snapshot("NIFTY")
        assert snap.pcr_change is None

    def test_greeks_are_computed_via_black_scholes_when_missing(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24500, atm=24500)
        snap = md.get_snapshot("NIFTY", expiry_date=dt.date.today() + dt.timedelta(days=2))
        atm_row = next(r for r in snap.strikes if r.strike == 24500)
        assert atm_row.ce_delta != 0.0
        assert 0.3 < atm_row.ce_delta < 0.7  # ATM delta should be roughly around 0.5

    def test_no_greeks_computed_without_expiry_date(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY")
        snap = md.get_snapshot("NIFTY")  # no expiry_date passed
        atm_row = next(r for r in snap.strikes if r.strike == 24500.0)
        assert atm_row.ce_delta == 0.0  # left at the coerced default, never guessed

    def test_latest_candle_and_vwap_come_from_real_archive(self, ti_db):
        insert_realistic_chain(ti_db, symbol="NIFTY")
        snap = md.get_snapshot("NIFTY")
        assert snap.latest_candle is not None
        assert snap.latest_candle["close"] > 0
        # NIFTY is a pure index -- no tradable volume feed, VWAP legitimately None
        assert snap.vwap is None
