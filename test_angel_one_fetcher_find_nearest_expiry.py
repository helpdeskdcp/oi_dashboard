"""
test_angel_one_fetcher_find_nearest_expiry.py -- regression tests for the
2026-08-20 fix to AngelOneFetcher.find_nearest_expiry() (app.py).

Bug: the previous implementation took list_available_expiries()'s first
(chronologically earliest) entry with NO check that it wasn't already in
the past. Confirmed via production logs: once Angel One's cached
instrument master still listed an already-expired near-week NIFTY
contract, this call site kept resolving that expired date for a full
trading day -- one week after expiry_intelligence.get_nearest_expiry()
(used by the Trading Intelligence engine, a completely separate call
site) had already correctly rolled to the real next expiry. This matters
a lot: find_nearest_expiry()'s result becomes run_symbol_loop()'s own
expiry_date_obj, which feeds generate_signal()'s Black-Scholes delta/
target/SL calc AND every paper-trading table's expiry_date_at_entry stamp.

Never touches a real broker session -- app.AngelOneFetcher.__new__(...)
bypasses __init__ (same pattern as test_angel_one_fetcher_greeks.py),
and `.instruments` is set directly to a synthetic instrument-master slice.
"""
import datetime as dt
import os

os.environ["SKIP_AUTOSTART"] = "1"

import pytest

import app
import expiry_intelligence


def _fetcher_with_instruments(rows):
    fetcher = app.AngelOneFetcher.__new__(app.AngelOneFetcher)
    fetcher.instruments = rows
    return fetcher


def _row(expiry_str, name="NIFTY", instrumenttype="OPTIDX"):
    return {"name": name, "instrumenttype": instrumenttype, "expiry": expiry_str}


class TestFindNearestExpirySkipsPastDates:
    def test_ignores_an_already_expired_entry_still_in_the_instrument_master(self, monkeypatch):
        monkeypatch.setattr(expiry_intelligence, "_today_ist", lambda: dt.date(2026, 8, 19))
        fetcher = _fetcher_with_instruments([
            _row("18AUG2026"),   # already expired as of "today" below
            _row("25AUG2026"),   # the real next expiry
            _row("01SEP2026"),
        ])
        assert fetcher.find_nearest_expiry("NIFTY") == "25AUG2026"

    def test_expiry_today_itself_still_counts_as_current(self, monkeypatch):
        monkeypatch.setattr(expiry_intelligence, "_today_ist", lambda: dt.date(2026, 8, 18))
        fetcher = _fetcher_with_instruments([_row("18AUG2026"), _row("25AUG2026")])
        assert fetcher.find_nearest_expiry("NIFTY") == "18AUG2026"

    def test_fully_stale_instrument_master_returns_none_not_an_expired_date(self, monkeypatch):
        """Fixed 2026-08-20 (Codex review, HIGH): a fully stale,
        unrefreshed instrument master (every listed date already past)
        now fails closed -- find_nearest_expiry() catches
        expiry_intelligence.ExpiryDataUnavailable and returns None, same
        as the no-instruments case, rather than selecting an expired
        contract as if it were current."""
        monkeypatch.setattr(expiry_intelligence, "_today_ist", lambda: dt.date(2026, 9, 10))
        fetcher = _fetcher_with_instruments([_row("18AUG2026"), _row("25AUG2026")])
        assert fetcher.find_nearest_expiry("NIFTY") is None

    def test_no_instruments_returns_none_not_an_exception(self):
        fetcher = _fetcher_with_instruments([])
        assert fetcher.find_nearest_expiry("NIFTY") is None

    def test_symbol_with_no_matching_rows_returns_none(self, monkeypatch):
        monkeypatch.setattr(expiry_intelligence, "_today_ist", lambda: dt.date(2026, 8, 19))
        fetcher = _fetcher_with_instruments([_row("25AUG2026", name="BANKNIFTY")])
        assert fetcher.find_nearest_expiry("NIFTY") is None

    def test_result_matches_expiry_intelligence_directly(self, monkeypatch):
        """The two call sites (this one and the Trading Intelligence
        engine's) must now agree -- the whole point of the fix. Regression
        guard against them silently diverging again in the future."""
        monkeypatch.setattr(expiry_intelligence, "_today_ist", lambda: dt.date(2026, 8, 19))
        fetcher = _fetcher_with_instruments([_row("18AUG2026"), _row("25AUG2026")])
        via_app = fetcher.find_nearest_expiry("NIFTY")
        via_expiry_intelligence = expiry_intelligence.get_nearest_expiry("NIFTY", fetcher)
        assert via_app == via_expiry_intelligence.strftime("%d%b%Y").upper()
