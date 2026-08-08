import datetime as dt

from agents.runtime import market_session as ms


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
