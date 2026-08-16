"""Milestone 25 Workstream 1 regression tests -- agents/timekeeping.py, the
canonical time contract, and the call sites it unified (ti_store.py,
virtual_trailing.py, candle_recorder.py, ai_live_snapshot.py,
production_watchdog.py, agents/runtime/market_session.py, app.py)."""
import datetime as dt

import pytest

from agents import timekeeping
from agents.runtime import market_session


class TestNowIstIsNaive:
    def test_now_ist_returns_a_naive_datetime(self):
        value = timekeeping.now_ist()
        assert value.tzinfo is None

    def test_now_ist_matches_the_real_utc_plus_5_30_wall_clock(self):
        # Independent of the OS's own configured local timezone -- computed
        # from dt.datetime.now(timezone.utc), not dt.datetime.now().
        expected = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + timekeeping.IST_OFFSET
        actual = timekeeping.now_ist()
        assert abs((actual - expected).total_seconds()) < 2

    def test_now_ist_iso_round_trips_through_fromisoformat_as_naive(self):
        s = timekeeping.now_ist_iso()
        parsed = dt.datetime.fromisoformat(s)
        assert parsed.tzinfo is None


class TestSingleCanonicalImplementation:
    """Milestone 25's actual fix: app.py and agents/runtime/market_session.py
    used to each keep their own hand-synchronized copy of this exact
    one-liner. Both must now be re-exports of the same function object."""

    def test_market_session_now_ist_is_the_canonical_implementation(self):
        assert market_session.now_ist is timekeeping.now_ist

    def test_app_now_ist_is_the_canonical_implementation(self):
        import app
        assert app.now_ist is timekeeping.now_ist


class TestUtcToIst:
    """The one sanctioned conversion path for an externally-sourced,
    tz-aware timestamp -- covers the midnight-boundary and UTC<->IST
    conversion regression categories."""

    def test_utc_to_ist_converts_a_known_instant(self):
        # 2026-08-15 03:45:00 UTC == 2026-08-15 09:15:00 IST (NSE open).
        utc_value = dt.datetime(2026, 8, 15, 3, 45, 0, tzinfo=dt.timezone.utc)
        ist_value = timekeeping.utc_to_ist(utc_value)
        assert ist_value == dt.datetime(2026, 8, 15, 9, 15, 0)
        assert ist_value.tzinfo is None

    def test_utc_to_ist_midnight_boundary_just_before(self):
        # 18:29 UTC on day N == 23:59 IST, same day N -- no date shift yet.
        utc_value = dt.datetime(2026, 8, 14, 18, 29, 0, tzinfo=dt.timezone.utc)
        ist_value = timekeeping.utc_to_ist(utc_value)
        assert ist_value == dt.datetime(2026, 8, 14, 23, 59, 0)

    def test_utc_to_ist_midnight_boundary_just_after(self):
        # 18:31 UTC on day N == 00:01 IST, day N+1 -- the date DOES shift.
        utc_value = dt.datetime(2026, 8, 14, 18, 31, 0, tzinfo=dt.timezone.utc)
        ist_value = timekeeping.utc_to_ist(utc_value)
        assert ist_value == dt.datetime(2026, 8, 15, 0, 1, 0)

    def test_utc_to_ist_accepts_any_aware_timezone_not_just_utc(self):
        est = dt.timezone(dt.timedelta(hours=-5))
        est_value = dt.datetime(2026, 8, 14, 23, 45, 0, tzinfo=est)   # == 04:45 UTC == 10:15 IST
        ist_value = timekeeping.utc_to_ist(est_value)
        assert ist_value == dt.datetime(2026, 8, 15, 10, 15, 0)

    def test_utc_to_ist_rejects_a_naive_datetime(self):
        naive = dt.datetime(2026, 8, 15, 9, 15, 0)
        with pytest.raises(ValueError, match="tz-aware"):
            timekeeping.utc_to_ist(naive)


class TestSessionBoundaries:
    """NSE/MCX session-open detection already took an explicit `at`
    parameter before Milestone 25 -- these boundary cases were previously
    untested. now_ist() itself is unaffected (session logic already used
    the (now-canonical) now_ist() convention); this locks in the boundary
    behavior against regression."""

    def test_nse_session_opens_exactly_at_09_15(self):
        at = dt.datetime(2026, 8, 17, 9, 15, 0)   # Monday
        assert market_session.is_nse_session_open(at=at) == (True, "")

    def test_nse_session_not_yet_open_one_second_before(self):
        at = dt.datetime(2026, 8, 17, 9, 14, 59)
        open_, reason = market_session.is_nse_session_open(at=at)
        assert open_ is False
        assert reason == "Outside trading hours"

    def test_nse_session_closes_exactly_at_15_40(self):
        at = dt.datetime(2026, 8, 17, 15, 40, 0)
        assert market_session.is_nse_session_open(at=at) == (True, "")

    def test_nse_session_closed_one_second_after_close(self):
        at = dt.datetime(2026, 8, 17, 15, 40, 1)
        open_, reason = market_session.is_nse_session_open(at=at)
        assert open_ is False
        assert reason == "Outside trading hours"

    def test_nse_session_closed_on_saturday(self):
        at = dt.datetime(2026, 8, 22, 10, 0, 0)   # Saturday
        assert market_session.is_nse_session_open(at=at) == (False, "Weekend")

    def test_unknown_exchange_is_honestly_reported_closed(self):
        at = dt.datetime(2026, 8, 17, 10, 0, 0)
        open_, reason = market_session.is_exchange_open("NYSE", at=at)
        assert open_ is False
        assert "unknown exchange" in reason


class TestPersistedTimestampsUseTheCanonicalClock:
    """Milestone 25's concrete fix: ti_store.py/virtual_trailing.py used to
    each write dt.datetime.now().isoformat() -- correct IST wall-clock
    time only because the writing server's OS timezone happened to be
    Asia/Kolkata (see performance_analytics.py's own M23 _timezone_note()
    docstring). Both must now delegate to timekeeping.now_ist_iso()."""

    def test_ti_store_now_is_within_a_couple_seconds_of_the_canonical_clock(self):
        from agents.trading_intelligence import ti_store
        written = dt.datetime.fromisoformat(ti_store._now())
        assert abs((written - timekeeping.now_ist()).total_seconds()) < 2

    def test_virtual_trailing_now_is_within_a_couple_seconds_of_the_canonical_clock(self):
        from agents.trading_intelligence import virtual_trailing
        written = dt.datetime.fromisoformat(virtual_trailing._now())
        assert abs((written - timekeeping.now_ist()).total_seconds()) < 2

    def test_ti_paper_trade_entry_time_persists_and_reloads_as_naive_ist(self, ti_db):
        from agents.trading_intelligence import ti_store
        ti_store.init_db()
        trade_id = ti_store.open_trade(
            symbol="NIFTY", strike=24500, direction="CE", entry_price=100.0,
            target_price=120.0, sl_price=90.0, qty=1,
        )
        row = ti_store.list_open_trades(symbol="NIFTY")[0]
        assert row["id"] == trade_id
        reloaded = dt.datetime.fromisoformat(row["entry_time"])
        assert reloaded.tzinfo is None
        assert abs((reloaded - timekeeping.now_ist()).total_seconds()) < 5

    def test_ai_live_snapshot_timestamp_field_uses_the_canonical_clock(self, monkeypatch):
        from agents.trading_intelligence import ai_live_snapshot
        monkeypatch.setattr(ai_live_snapshot.data_access, "latest_cycle", lambda symbol: None)
        # latest_cycle() returning None short-circuits to a plain None
        # snapshot -- this test only needs to prove the module imports
        # and wires timekeeping correctly, not exercise the full snapshot
        # (that's ai_live_snapshot's own existing test file's job).
        assert ai_live_snapshot.build_ai_live_snapshot("NIFTY") is None
        assert not hasattr(ai_live_snapshot, "dt")   # Milestone 25: dead `import datetime as dt` removed


class TestCandleFreshnessUsesTheCanonicalClock:
    def test_candle_lag_seconds_default_now_is_ist_not_server_local(self, ti_db, monkeypatch):
        from agents.trading_intelligence import candle_recorder
        candle_recorder.init_db()
        # A 1m candle only CLOSES (and becomes readable via
        # last_candle_time()) once a later tick lands in the NEXT bucket --
        # two ticks two minutes apart, both anchored to the canonical
        # clock, so the closed candle's own timestamp is ~90s in the past.
        base = timekeeping.now_ist() - dt.timedelta(minutes=2)
        candle_recorder.append_tick("NIFTY", base, 24500.0)
        candle_recorder.append_tick("NIFTY", base + dt.timedelta(minutes=1), 24505.0)
        lag = candle_recorder.candle_lag_seconds("NIFTY", "1m")
        # Should read as roughly 2 minutes stale against the canonical
        # clock (bucket-boundary flooring makes the exact figure fuzzy),
        # not drift by whatever offset a server-local dt.datetime.now()
        # would introduce if the OS timezone weren't IST -- a wrong clock
        # here would put this hundreds/thousands of seconds off, not
        # within one bucket-width of 120s.
        assert lag is not None
        assert 60 <= lag <= 200
