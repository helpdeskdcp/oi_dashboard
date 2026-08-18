"""
test_angel_one_fetcher_greeks.py -- regression tests for AngelOneFetcher.
get_option_greeks() (app.py) and its permanent-failure cooldown fix.

Post-launch fix: every MCX commodity symbol gets errorcode "AB9019" ("No
Data Available") from Angel One's optionGreek endpoint on every single
attempt, confirmed via 24h+ of production logs -- never a transient blip.
Retrying that at the normal 60s throttle forever wastes a real network
round-trip (and a noisy SDK-level error log) for data that will never
arrive. get_option_greeks() now returns (greeks_by_strike, errorcode) so
the call site can back off much longer once it's confirmed permanent.

Constructs AngelOneFetcher with a FAKE client (a plain object exposing an
.optionGreek() method) and a fresh `_last_login_time` -- never touches a
real broker session or attempts a real login (matches every other test in
this repo's own hard "never touch a live broker in tests" rule).
"""
import os
import time

os.environ["SKIP_AUTOSTART"] = "1"

import pytest

import app


def _fetcher_with_fake_client(optiongreek_fn):
    fetcher = app.AngelOneFetcher.__new__(app.AngelOneFetcher)
    fetcher.client = type("FakeClient", (), {"optionGreek": staticmethod(optiongreek_fn)})()
    fetcher._last_login_time = time.time()   # recent -- _ensure_session_fresh() no-ops, never relogs in
    fetcher._last_login_attempt = None
    fetcher._login_lock = __import__("threading").Lock()
    return fetcher


class TestGetOptionGreeksReturnShape:
    def test_success_returns_greeks_and_no_errorcode(self):
        def fake_optiongreek(params):
            return {
                "status": True, "errorcode": "", "data": [
                    {"strikePrice": "24500", "optionType": "CE", "delta": "0.5", "gamma": "0.01",
                     "theta": "-1.2", "vega": "3.4", "impliedVolatility": "15.5"},
                ],
            }
        fetcher = _fetcher_with_fake_client(fake_optiongreek)

        greeks, errorcode = fetcher.get_option_greeks("NIFTY", "28AUG2026")

        assert errorcode is None
        assert greeks[(24500.0, "CE")]["delta"] == 0.5
        assert greeks[(24500.0, "CE")]["iv"] == 15.5

    def test_no_data_available_returns_the_real_errorcode(self):
        """The exact response Angel One returns for every MCX commodity
        symbol, confirmed via production logs -- CRUDEOIL/CRUDEOILM/
        SILVER/SILVERM/NATURALGAS/NATGASMINI/GOLD/GOLDM, every attempt."""
        def fake_optiongreek(params):
            return {"status": False, "message": "No Data Available", "errorcode": "AB9019", "data": None}
        fetcher = _fetcher_with_fake_client(fake_optiongreek)

        greeks, errorcode = fetcher.get_option_greeks("CRUDEOIL", "17SEP2026")

        assert greeks == {}
        assert errorcode == "AB9019"
        assert errorcode == app.AngelOneFetcher.GREEKS_NO_DATA_ERRORCODE

    def test_other_failure_errorcode_is_propagated_too(self):
        def fake_optiongreek(params):
            return {"status": False, "message": "Something else went wrong", "errorcode": "AB1234", "data": None}
        fetcher = _fetcher_with_fake_client(fake_optiongreek)

        greeks, errorcode = fetcher.get_option_greeks("NIFTY", "28AUG2026")

        assert greeks == {}
        assert errorcode == "AB1234"
        assert errorcode != app.AngelOneFetcher.GREEKS_NO_DATA_ERRORCODE

    def test_exception_returns_empty_and_no_errorcode_never_raises(self):
        def fake_optiongreek(params):
            raise RuntimeError("network blip")
        fetcher = _fetcher_with_fake_client(fake_optiongreek)

        greeks, errorcode = fetcher.get_option_greeks("NIFTY", "28AUG2026")

        assert greeks == {}
        assert errorcode is None

    def test_no_client_returns_empty_without_calling_anything(self):
        fetcher = app.AngelOneFetcher.__new__(app.AngelOneFetcher)
        fetcher.client = None
        fetcher._last_login_time = None
        # Recent enough that _ensure_session_fresh()'s "need relogin" branch
        # skips the actual attempt (LOGIN_RETRY_COOLDOWN_SECONDS not yet
        # elapsed) -- never a real login call, matching every other test
        # here's own "never touch a live broker session" contract.
        fetcher._last_login_attempt = time.time()
        fetcher._login_lock = __import__("threading").Lock()

        greeks, errorcode = fetcher.get_option_greeks("NIFTY", "28AUG2026")

        assert greeks == {}
        assert errorcode is None

    def test_malformed_rows_are_skipped_not_fatal(self):
        def fake_optiongreek(params):
            return {
                "status": True, "errorcode": "", "data": [
                    {"strikePrice": "not-a-number", "optionType": "CE"},   # malformed -- skipped
                    {"strikePrice": "24500", "optionType": "PE", "delta": "-0.5", "gamma": "0.01",
                     "theta": "-1.1", "vega": "3.0", "impliedVolatility": "16.0"},
                ],
            }
        fetcher = _fetcher_with_fake_client(fake_optiongreek)

        greeks, errorcode = fetcher.get_option_greeks("NIFTY", "28AUG2026")

        assert errorcode is None
        assert (24500.0, "PE") in greeks
        assert len(greeks) == 1


class TestCooldownSelectionLogic:
    """The exact conditional the call site (app.py's main data-fetch loop)
    uses to pick between the normal 60s throttle and the much longer
    permanent-failure cooldown -- verified standalone since the call site
    itself lives inside a large, untested background loop function (no
    existing test harness for it), matching this repo's own established
    pattern of testing extracted logic rather than the whole loop."""

    def _cooldown_for(self, entry):
        return (
            app.AngelOneFetcher.GREEKS_PERMANENT_FAILURE_COOLDOWN_SECONDS
            if entry.get("errorcode") == app.AngelOneFetcher.GREEKS_NO_DATA_ERRORCODE
            else app.AngelOneFetcher.GREEKS_FETCH_THROTTLE_SECONDS
        )

    def test_permanent_no_data_failure_gets_the_long_cooldown(self):
        entry = {"ts": time.time(), "data": {}, "errorcode": "AB9019"}
        assert self._cooldown_for(entry) == app.AngelOneFetcher.GREEKS_PERMANENT_FAILURE_COOLDOWN_SECONDS
        assert self._cooldown_for(entry) == 3600

    def test_success_keeps_the_normal_throttle(self):
        entry = {"ts": time.time(), "data": {(100.0, "CE"): {}}, "errorcode": None}
        assert self._cooldown_for(entry) == app.AngelOneFetcher.GREEKS_FETCH_THROTTLE_SECONDS
        assert self._cooldown_for(entry) == 60

    def test_other_failure_reason_keeps_the_normal_throttle_not_the_long_one(self):
        """Only the confirmed-permanent AB9019 answer gets the long
        cooldown -- a genuinely transient failure (network blip, a
        different Angel One error) must keep retrying soon, not be
        silently treated as permanent too."""
        entry = {"ts": time.time(), "data": {}, "errorcode": "AB1234"}
        assert self._cooldown_for(entry) == app.AngelOneFetcher.GREEKS_FETCH_THROTTLE_SECONDS

    def test_never_fetched_yet_keeps_the_normal_throttle(self):
        entry = {}
        assert self._cooldown_for(entry) == app.AngelOneFetcher.GREEKS_FETCH_THROTTLE_SECONDS
