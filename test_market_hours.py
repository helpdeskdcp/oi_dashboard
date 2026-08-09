"""
test_market_hours.py -- regression tests for the 2026-08-03 NSE/MCX
market-hours revision: is_market_open()/MARKET_HOURS/_resolve_market_hours()/
_mcx_nonagri_close() in app.py.

is_market_open() is a pure function of (cfg, now_ist()) -- no database
needed, so this file only needs SKIP_AUTOSTART=1 + import app, matching
test_paper_orders_phase3.py's own NIFTY_CFG convention but without its
heavier DB-backed client fixture.
"""
import os
os.environ["SKIP_AUTOSTART"] = "1"

import datetime as dt
from unittest.mock import patch

import pytest

import app

NIFTY_CFG = app.SYMBOLS["NIFTY"]
VIX_CFG = app.SYMBOLS["INDIA VIX"]
GOLD_CFG = app.SYMBOLS["GOLD"]
CRUDEOIL_CFG = app.SYMBOLS["CRUDEOIL"]

MONDAY = dt.date(2026, 8, 10)   # a real Monday, matching the "at" fixtures used elsewhere in this suite
SATURDAY = dt.date(2026, 8, 8)


def _at(date, hour, minute):
    return dt.datetime(date.year, date.month, date.day, hour, minute)


class TestEquityFnOExtendedClose:
    """NIFTY/BANKNIFTY/FINNIFTY/MIDCPNIFTY/SENSEX -- all "index_option" --
    are the Equity F&O segment, now closing at 15:40 (was 15:30)."""

    def test_open_at_1535(self):
        with patch.object(app, "now_ist", lambda: _at(MONDAY, 15, 35)):
            assert app.is_market_open(NIFTY_CFG) == (True, "")

    def test_open_at_exact_new_close_boundary_1540(self):
        with patch.object(app, "now_ist", lambda: _at(MONDAY, 15, 40)):
            assert app.is_market_open(NIFTY_CFG) == (True, "")

    def test_closed_at_1541(self):
        with patch.object(app, "now_ist", lambda: _at(MONDAY, 15, 41)):
            open_, reason = app.is_market_open(NIFTY_CFG)
            assert open_ is False
            assert reason == "Outside trading hours"

    def test_all_index_option_symbols_share_the_new_close(self):
        for symbol in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
            assert app.SYMBOLS[symbol]["type"] == "index_option"
        assert app.MARKET_HOURS["index_option"] == (9, 15, 15, 40)


class TestIndexSpotUnchanged:
    """INDIA VIX ("index_spot") is not F&O-traded -- its session tracks
    the plain cash/normal window and is NOT affected by the F&O
    close-time extension."""

    def test_closed_at_1535_unlike_index_option(self):
        with patch.object(app, "now_ist", lambda: _at(MONDAY, 15, 35)):
            open_, reason = app.is_market_open(VIX_CFG)
            assert open_ is False
            assert reason == "Outside trading hours"

    def test_open_at_exact_1530_boundary(self):
        with patch.object(app, "now_ist", lambda: _at(MONDAY, 15, 30)):
            assert app.is_market_open(VIX_CFG) == (True, "")

    def test_market_hours_entry_unchanged(self):
        assert app.MARKET_HOURS["index_spot"] == (9, 15, 15, 30)


class TestMcxNonAgriSeasonalClose:
    """GOLD/GOLDM/SILVER/SILVERM/CRUDEOIL/CRUDEOILM/NATURALGAS/NATGASMINI
    are all "commodity_nonagri" (metals/energy/bullion) -- close shifts
    with the DST-linked seasonal window: 23:55 IST during it, 23:30 IST
    outside it."""

    def test_all_currently_tracked_mcx_symbols_are_nonagri(self):
        for symbol in ("CRUDEOIL", "CRUDEOILM", "NATURALGAS", "NATGASMINI",
                        "GOLD", "GOLDM", "SILVER", "SILVERM"):
            assert app.SYMBOLS[symbol]["type"] == "commodity_nonagri"

    def test_summer_window_open_at_2350(self):
        # 2026-07-15 -- squarely inside the DST-linked window (2nd Sun
        # March through 1st Sun Nov)
        with patch.object(app, "now_ist", lambda: dt.datetime(2026, 7, 15, 23, 50)):
            assert app.is_market_open(GOLD_CFG) == (True, "")

    def test_summer_window_closed_at_exact_2355_plus_one_minute(self):
        with patch.object(app, "now_ist", lambda: dt.datetime(2026, 7, 15, 23, 56)):
            open_, reason = app.is_market_open(GOLD_CFG)
            assert open_ is False
            assert reason == "Outside trading hours"

    def test_winter_window_closed_at_2340(self):
        # 2026-12-15 -- outside the DST-linked window
        with patch.object(app, "now_ist", lambda: dt.datetime(2026, 12, 15, 23, 40)):
            open_, reason = app.is_market_open(CRUDEOIL_CFG)
            assert open_ is False
            assert reason == "Outside trading hours"

    def test_winter_window_open_at_2325(self):
        with patch.object(app, "now_ist", lambda: dt.datetime(2026, 12, 15, 23, 25)):
            assert app.is_market_open(CRUDEOIL_CFG) == (True, "")

    def test_dst_start_boundary_2026(self):
        # 2nd Sunday of March 2026 is 2026-03-08 -- the seasonal window
        # starts THAT calendar day (tested via _mcx_nonagri_close()
        # directly, bypassing is_market_open()'s weekday gate, since the
        # 2nd-Sunday-of-March boundary date is, by definition, a Sunday
        # -- MCX doesn't trade that day regardless of the seasonal window).
        assert app._mcx_nonagri_close(dt.datetime(2026, 3, 8, 23, 50)) == (23, 55)
        assert app._mcx_nonagri_close(dt.datetime(2026, 3, 7, 23, 50)) == (23, 30)

    def test_dst_end_boundary_2026(self):
        # 1st Sunday of November 2026 is 2026-11-01 -- the window ends
        # (exclusive) that day, i.e. winter hours resume ON 2026-11-01.
        assert app._mcx_nonagri_close(dt.datetime(2026, 10, 31, 23, 50)) == (23, 55)
        assert app._mcx_nonagri_close(dt.datetime(2026, 11, 1, 23, 40)) == (23, 30)


class TestMcxAgriDocumentedButUnused:
    """No agricultural commodity is currently tracked by this app, but
    the timing bucket itself must exist and be correct for future use."""

    def test_commodity_agri_hours(self):
        assert app.MARKET_HOURS["commodity_agri"] == (9, 0, 17, 0)

    def test_no_symbol_currently_uses_commodity_agri(self):
        assert not any(cfg["type"] == "commodity_agri" for cfg in app.SYMBOLS.values())


class TestFnoCashStockAndNonFnoStockDocumentedButUnused:
    """No individual stock symbol is currently tracked by this app
    (only indices and MCX commodities) -- these buckets exist for
    documentation/future-use correctness."""

    def test_fno_cash_stock_hours_is_continuous_close_1515(self):
        assert app.MARKET_HOURS["fno_cash_stock"] == (9, 15, 15, 15)

    def test_non_fno_stock_hours_unchanged(self):
        assert app.MARKET_HOURS["non_fno_stock"] == (9, 15, 15, 30)

    def test_no_symbol_currently_uses_either_stock_type(self):
        assert not any(cfg["type"] in ("fno_cash_stock", "non_fno_stock") for cfg in app.SYMBOLS.values())


class TestCommodityTypesMembership:
    """COMMODITY_TYPES is the "is this symbol MCX" check used by broker
    token-resolution logic (find_option_token, is_expiry_today,
    get_underlying_token_for_candles, build_strike_rows,
    run_symbol_loop) -- must match BOTH commodity subtypes, not just
    non-agri, so a future agri commodity wouldn't silently fall through
    those checks."""

    def test_nonagri_symbol_matches(self):
        assert GOLD_CFG["type"] in app.COMMODITY_TYPES

    def test_index_symbol_does_not_match(self):
        assert NIFTY_CFG["type"] not in app.COMMODITY_TYPES

    def test_both_subtypes_present(self):
        assert set(app.COMMODITY_TYPES) == {"commodity_agri", "commodity_nonagri"}


class TestWeekendUnaffected:
    def test_weekend_closed_regardless_of_type(self):
        for cfg in (NIFTY_CFG, VIX_CFG, GOLD_CFG):
            with patch.object(app, "now_ist", lambda: _at(SATURDAY, 12, 0)):
                open_, reason = app.is_market_open(cfg)
                assert open_ is False
                assert reason == "Weekend"


class TestResolveMarketHoursHelper:
    """_resolve_market_hours() is the single source of truth shared by
    is_market_open() and the intraday auto-square-off buffer calculation
    (app.py's update_paper_orders) -- both must always agree on the
    real close time, including the seasonal MCX shift."""

    def test_static_type_returns_dict_value_unchanged(self):
        now = _at(MONDAY, 10, 0)
        assert app._resolve_market_hours(NIFTY_CFG, now) == (9, 15, 15, 40)

    def test_mcx_nonagri_returns_seasonal_close_summer(self):
        now = dt.datetime(2026, 7, 15, 10, 0)
        assert app._resolve_market_hours(GOLD_CFG, now) == (9, 0, 23, 55)

    def test_mcx_nonagri_returns_seasonal_close_winter(self):
        now = dt.datetime(2026, 12, 15, 10, 0)
        assert app._resolve_market_hours(GOLD_CFG, now) == (9, 0, 23, 30)
