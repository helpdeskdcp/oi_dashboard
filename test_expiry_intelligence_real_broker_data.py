"""
test_expiry_intelligence_real_broker_data.py -- Expiry-integrity follow-up
(2026-08-24): verifies expiry_intelligence.get_nearest_expiry() and
app.py's AngelOneFetcher.find_option_token() algorithm against the REAL,
live-cached Angel One instrument master (instrument_master.json,
gitignored -- a live-environment-only artifact, never checked in, never
present in CI), not a synthetic mirror.

This exists specifically to answer a reported concern: a NIFTY signal on
market date 2026-08-24 allegedly showed "Expiry: 26-Aug-2026" while the
real broker-listed nearest expiry is 25-Aug-2026. test_expiry_intelligence.py's
own TestRealNiftyAug2026ExpiryRegression already locks in the correct
behavior using synthetic dates that MIRROR this real evidence (matching
that file's own "never touch the real cache file" convention) -- this
file goes one step further and reads the actual cache file directly, so
the regression is proven against real data whenever that data is
available on the host running the suite.

Deliberately does NOT import app.py or instantiate AngelOneFetcher (that
class's __init__ reaches toward a live broker session -- see this
project's own established "never touch the broker from a test process"
caution). Instead this mirrors the exact, small, pure-data algorithms
AngelOneFetcher.list_available_expiries()/find_option_token() use (both
confirmed via direct reading of app.py at the time this file was
written) -- reading only the already-cached JSON file on disk, no
network call, no login, no broker session.

Skipped entirely (not failed) when instrument_master.json isn't present
-- e.g. a CI checkout, or a dev machine that has never run the live app.
"""
import datetime as dt
import json
import os
import re

import pytest

import expiry_intelligence as ei

INSTRUMENT_MASTER_PATH = os.path.join(os.path.dirname(__file__), "instrument_master.json")

pytestmark = pytest.mark.skipif(
    not os.path.exists(INSTRUMENT_MASTER_PATH),
    reason="instrument_master.json not present on this host (gitignored, live-environment-only artifact)",
)


@pytest.fixture(scope="module")
def real_instruments():
    with open(INSTRUMENT_MASTER_PATH) as f:
        return json.load(f)


class _RealDataFetcher:
    """Mirrors AngelOneFetcher.list_available_expiries()'s exact filter/
    parse/sort logic (app.py), fed from the real cached instrument list
    -- duck-types the one method expiry_intelligence.py's resolver
    functions call, the same contract test_expiry_intelligence.py's own
    FakeFetcher already establishes."""

    def __init__(self, instruments):
        self.instruments = instruments

    def list_available_expiries(self, symbol):
        candidates = [
            row for row in self.instruments
            if row.get("name") == symbol and row.get("instrumenttype") in ("OPTIDX", "OPTSTK", "OPTFUT")
            and row.get("expiry")
        ]
        unique_expiries = {row["expiry"] for row in candidates}
        parsed = []
        for exp_str in unique_expiries:
            try:
                parsed.append(dt.datetime.strptime(exp_str, "%d%b%Y").date())
            except ValueError:
                continue
        parsed.sort()
        return parsed


def _parse_expiry(s: str) -> dt.date:
    return dt.datetime.strptime(s, "%d%b%Y").date()


def _find_option_token(instruments, symbol, strike, opt_type, *, exch_seg="NFO", today):
    """Mirrors AngelOneFetcher.find_option_token()'s exact algorithm
    (app.py) -- same primary filter, same fallback regex extraction, same
    nearest-upcoming-expiry sort. Pure function over already-loaded data,
    no broker call."""
    candidates = [
        row for row in instruments
        if row.get("name") == symbol
        and row.get("exch_seg") == exch_seg
        and row.get("instrumenttype") in ("OPTIDX", "OPTSTK")
        and row.get("symbol", "").endswith(opt_type)
        and str(row.get("strike", "")).replace(".000000", "") == str(strike * 100)
    ]
    if not candidates:
        strike_str = str(int(strike))
        candidates = []
        for row in instruments:
            if row.get("name") != symbol or row.get("exch_seg") != exch_seg:
                continue
            sym = row.get("symbol", "")
            m = re.search(r"(\d+)" + re.escape(opt_type) + r"$", sym)
            if m and m.group(1) == strike_str:
                candidates.append(row)
    if not candidates:
        return None, None
    upcoming = [c for c in candidates if _parse_expiry(c.get("expiry", "")) >= today]
    pool = upcoming or candidates
    pool.sort(key=lambda r: _parse_expiry(r.get("expiry", "")))
    chosen = pool[0]
    return chosen.get("token"), chosen.get("symbol")


class TestRealInstrumentMasterNiftyAug2026:
    """The exact reported scenario, against real broker-listed data."""

    def test_nearest_expiry_for_2026_08_24_is_the_25th_not_the_26th(self, real_instruments):
        fetcher = _RealDataFetcher(real_instruments)
        selected = ei.get_nearest_expiry("NIFTY", fetcher, today=dt.date(2026, 8, 24))
        assert selected == dt.date(2026, 8, 25)
        assert selected != dt.date(2026, 8, 26)

    def test_no_such_thing_as_a_26aug2026_nifty_contract_exists(self, real_instruments):
        matches = [
            r for r in real_instruments
            if r.get("name") == "NIFTY" and r.get("instrumenttype") == "OPTIDX" and r.get("expiry") == "26AUG2026"
        ]
        assert matches == []

    def test_the_24500_ce_contract_for_the_real_nearest_expiry_exists_and_matches(self, real_instruments):
        token, trading_symbol = _find_option_token(
            real_instruments, "NIFTY", 24500, "CE", today=dt.date(2026, 8, 24),
        )
        assert token is not None
        assert trading_symbol is not None
        assert trading_symbol == "NIFTY25AUG2624500CE"
        # find_option_token()'s own algorithm and expiry_intelligence's own
        # resolver must agree on the SAME expiry for the SAME symbol at the
        # SAME moment -- the "two independent calculations could diverge"
        # risk flagged during the read-only audit, checked directly here.
        # Angel One's trading_symbol encodes a 2-digit year (DDMMMYY,
        # e.g. "25AUG26"), distinct from the instrument master's own
        # `expiry` field (4-digit year, "25AUG2026") -- confirmed against
        # the real trading_symbol string, not assumed.
        m = re.match(r"^NIFTY(\d{2}[A-Z]{3}\d{2})\d+CE$", trading_symbol)
        assert m is not None
        contract_expiry = dt.datetime.strptime(m.group(1), "%d%b%y").date()
        fetcher = _RealDataFetcher(real_instruments)
        resolver_expiry = ei.get_nearest_expiry("NIFTY", fetcher, today=dt.date(2026, 8, 24))
        assert contract_expiry == resolver_expiry == dt.date(2026, 8, 25)
