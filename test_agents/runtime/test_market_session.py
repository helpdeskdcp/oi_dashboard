import datetime as dt

from agents.runtime import market_session as ms


class TestExchangeMap:
    def test_all_nse_index_symbols_mapped_to_nse(self):
        for sym in ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX"):
            assert ms.EXCHANGE_MAP[sym] == "NSE"

    def test_all_mcx_commodity_symbols_mapped_to_mcx(self):
        for sym in ("NATURALGAS", "NATGASMINI", "CRUDEOIL", "CRUDEOILM",
                    "GOLD", "GOLDM", "SILVER", "SILVERM"):
            assert ms.EXCHANGE_MAP[sym] == "MCX"


class TestIsNseSessionOpen:
    def test_weekday_during_hours_is_open(self):
        # 2026-08-06 is a Thursday
        at = dt.datetime(2026, 8, 6, 10, 0)
        open_, reason = ms.is_nse_session_open(at=at)
        assert open_ is True
        assert reason == ""

    def test_weekday_before_open_is_closed(self):
        at = dt.datetime(2026, 8, 6, 8, 0)
        open_, reason = ms.is_nse_session_open(at=at)
        assert open_ is False
        assert reason == "Outside trading hours"

    def test_weekday_after_close_is_closed(self):
        at = dt.datetime(2026, 8, 6, 16, 0)
        open_, _reason = ms.is_nse_session_open(at=at)
        assert open_ is False

    def test_saturday_is_closed(self):
        at = dt.datetime(2026, 8, 8, 10, 0)  # Saturday
        open_, reason = ms.is_nse_session_open(at=at)
        assert open_ is False
        assert reason == "Weekend"

    def test_sunday_is_closed(self):
        at = dt.datetime(2026, 8, 9, 10, 0)  # Sunday
        open_, reason = ms.is_nse_session_open(at=at)
        assert open_ is False
        assert reason == "Weekend"

    def test_exact_open_boundary_is_open(self):
        at = dt.datetime(2026, 8, 6, 9, 15)
        assert ms.is_nse_session_open(at=at)[0] is True

    def test_exact_close_boundary_is_open(self):
        at = dt.datetime(2026, 8, 6, 15, 40)
        assert ms.is_nse_session_open(at=at)[0] is True


class TestIsMcxSessionOpen:
    def test_weekday_during_hours_is_open(self):
        # 2026-08-06 is a Thursday, within the DST-linked summer window
        # (2nd Sunday of March through 1st Sunday of November) -> close
        # should be mcx_session_config.summer_close() (23:55), not the
        # winter 23:30.
        at = dt.datetime(2026, 8, 6, 20, 0)
        open_, reason = ms.is_mcx_session_open(at=at)
        assert open_ is True
        assert reason == ""

    def test_weekday_before_open_is_closed(self):
        at = dt.datetime(2026, 8, 6, 8, 0)
        open_, reason = ms.is_mcx_session_open(at=at)
        assert open_ is False
        assert reason == "Outside MCX trading hours"

    def test_summer_window_stays_open_past_the_winter_close_time(self):
        at = dt.datetime(2026, 8, 6, 23, 40)  # after 23:30, before 23:55
        assert ms.is_mcx_session_open(at=at)[0] is True

    def test_summer_window_closes_after_23_55(self):
        at = dt.datetime(2026, 8, 6, 23, 59)
        assert ms.is_mcx_session_open(at=at)[0] is False

    def test_winter_window_closes_at_23_30(self):
        # 2026-01-15 is outside the DST-linked window -> winter_close (23:30).
        at = dt.datetime(2026, 1, 15, 23, 40)
        assert ms.is_mcx_session_open(at=at)[0] is False

    def test_saturday_is_closed(self):
        at = dt.datetime(2026, 8, 8, 10, 0)  # Saturday
        open_, reason = ms.is_mcx_session_open(at=at)
        assert open_ is False
        assert reason == "Weekend"


class TestIsExchangeOpen:
    def test_dispatches_to_nse_check(self, monkeypatch):
        monkeypatch.setattr(ms, "is_nse_session_open", lambda **kw: (True, ""))
        assert ms.is_exchange_open("NSE") == (True, "")

    def test_dispatches_to_mcx_check(self, monkeypatch):
        monkeypatch.setattr(ms, "is_mcx_session_open", lambda **kw: (True, ""))
        assert ms.is_exchange_open("MCX") == (True, "")

    def test_unknown_exchange_is_closed_with_a_reason(self):
        open_, reason = ms.is_exchange_open("BSE_COMMODITY")
        assert open_ is False
        assert "BSE_COMMODITY" in reason


class TestActiveSymbols:
    def test_returns_only_symbols_whose_exchange_is_open(self, monkeypatch):
        monkeypatch.setattr(ms, "is_nse_session_open", lambda **kw: (False, "Outside trading hours"))
        monkeypatch.setattr(ms, "is_mcx_session_open", lambda **kw: (True, ""))
        result = ms.active_symbols(["NIFTY", "CRUDEOIL", "GOLD"])
        assert result == ["CRUDEOIL", "GOLD"]

    def test_empty_when_nothing_open(self, monkeypatch):
        monkeypatch.setattr(ms, "is_nse_session_open", lambda **kw: (False, "Weekend"))
        monkeypatch.setattr(ms, "is_mcx_session_open", lambda **kw: (False, "Weekend"))
        assert ms.active_symbols(["NIFTY", "CRUDEOIL"]) == []

    def test_preserves_input_order(self, monkeypatch):
        monkeypatch.setattr(ms, "is_nse_session_open", lambda **kw: (True, ""))
        monkeypatch.setattr(ms, "is_mcx_session_open", lambda **kw: (True, ""))
        assert ms.active_symbols(["GOLD", "NIFTY", "CRUDEOIL"]) == ["GOLD", "NIFTY", "CRUDEOIL"]


class TestAnyWatchedExchangeOpen:
    def test_true_when_only_mcx_is_open(self, monkeypatch):
        monkeypatch.setattr(ms, "is_nse_session_open", lambda **kw: (False, "Outside trading hours"))
        monkeypatch.setattr(ms, "is_mcx_session_open", lambda **kw: (True, ""))
        assert ms.any_watched_exchange_open(["NIFTY", "SENSEX", "CRUDEOIL"]) is True

    def test_false_when_both_closed(self, monkeypatch):
        monkeypatch.setattr(ms, "is_nse_session_open", lambda **kw: (False, "Weekend"))
        monkeypatch.setattr(ms, "is_mcx_session_open", lambda **kw: (False, "Weekend"))
        assert ms.any_watched_exchange_open(["NIFTY", "CRUDEOIL"]) is False


class TestSecondsUntilNextOpen:
    def test_before_open_same_day_returns_positive_gap(self):
        at = dt.datetime(2026, 8, 6, 8, 0)  # Thursday 08:00
        seconds = ms.seconds_until_next_open(at=at)
        assert 0 < seconds <= 75 * 60  # up to 09:15 same day

    def test_after_close_returns_gap_to_next_day(self):
        at = dt.datetime(2026, 8, 6, 16, 0)  # Thursday 16:00
        seconds = ms.seconds_until_next_open(at=at)
        assert seconds > 0

    def test_friday_evening_skips_the_weekend(self):
        at = dt.datetime(2026, 8, 7, 16, 0)  # Friday 16:00
        seconds = ms.seconds_until_next_open(at=at)
        next_open = at + dt.timedelta(seconds=seconds)
        assert next_open.weekday() == 0  # Monday
