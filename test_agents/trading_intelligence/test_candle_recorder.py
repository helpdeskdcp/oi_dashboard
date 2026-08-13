"""
test_agents/trading_intelligence/test_candle_recorder.py -- Milestone 20,
Phase 6: regression tests for candle_recorder.py, the in-process 1m/3m/5m
candle builder that closes the "archive only updates once a day" gap
structure_alerts.py/multi_timeframe.py were silently relying on. Pure
unit tests -- feeds synthetic ticks directly via append_tick(), no real
broker/app.py dependency (see conftest.py's own note on why importing
app.py in a test process is a landmine).
"""
import datetime as dt

from agents.trading_intelligence import candle_recorder as cr


def _t(seconds_offset: int) -> dt.datetime:
    base = dt.datetime(2026, 8, 13, 9, 0, 0)
    return base + dt.timedelta(seconds=seconds_offset)


class TestAppendTickAggregation:
    def test_single_tick_forms_an_open_bucket_not_yet_completed(self, ti_db):
        cr.append_tick("NIFTY", _t(0), 24500.0)
        assert cr.get_recent_candles("NIFTY", "1m") == []

    def test_a_tick_in_a_new_minute_closes_the_previous_bucket(self, ti_db):
        cr.append_tick("NIFTY", _t(0), 24500.0)
        cr.append_tick("NIFTY", _t(20), 24510.0)
        cr.append_tick("NIFTY", _t(40), 24490.0)
        cr.append_tick("NIFTY", _t(65), 24505.0)   # crosses into the next 1m bucket

        candles = cr.get_recent_candles("NIFTY", "1m")
        assert len(candles) == 1
        c = candles[0]
        assert c["datetime"] == _t(0).replace(second=0, microsecond=0)
        assert c["open"] == 24500.0
        assert c["high"] == 24510.0
        assert c["low"] == 24490.0
        assert c["close"] == 24490.0   # last tick INSIDE the closed bucket, not the one that closed it

    def test_high_low_close_update_correctly_within_one_bucket(self, ti_db):
        cr.append_tick("NIFTY", _t(0), 100.0)
        cr.append_tick("NIFTY", _t(10), 105.0)
        cr.append_tick("NIFTY", _t(20), 95.0)
        cr.append_tick("NIFTY", _t(30), 102.0)
        cr.append_tick("NIFTY", _t(65), 999.0)   # closes the bucket

        c = cr.get_recent_candles("NIFTY", "1m")[0]
        assert c["open"] == 100.0
        assert c["high"] == 105.0
        assert c["low"] == 95.0
        assert c["close"] == 102.0

    def test_multiple_timeframes_recorded_from_the_same_ticks(self, ti_db):
        for i in range(0, 320, 15):
            cr.append_tick("NIFTY", _t(i), 24500.0 + i)
        assert len(cr.get_recent_candles("NIFTY", "1m")) >= 4
        assert len(cr.get_recent_candles("NIFTY", "3m")) >= 1
        assert len(cr.get_recent_candles("NIFTY", "5m")) >= 1

    def test_symbols_are_isolated(self, ti_db):
        cr.append_tick("NIFTY", _t(0), 100.0)
        cr.append_tick("NIFTY", _t(65), 100.0)
        cr.append_tick("BANKNIFTY", _t(0), 50000.0)
        cr.append_tick("BANKNIFTY", _t(65), 50000.0)
        assert len(cr.get_recent_candles("NIFTY", "1m")) == 1
        assert len(cr.get_recent_candles("BANKNIFTY", "1m")) == 1

    def test_invalid_ltp_is_ignored_not_raised(self, ti_db):
        cr.append_tick("NIFTY", _t(0), 0)
        cr.append_tick("NIFTY", _t(0), None)
        cr.append_tick("NIFTY", _t(0), -5)
        assert cr.get_recent_candles("NIFTY", "1m") == []


class TestPersistence:
    def test_closed_candle_is_written_through_to_the_db(self, ti_db):
        cr.append_tick("NIFTY", _t(0), 24500.0)
        cr.append_tick("NIFTY", _t(65), 24505.0)

        rows = cr._load_from_db("NIFTY", "1m", limit=10)
        assert len(rows) == 1
        assert rows[0]["open"] == 24500.0

    def test_get_recent_candles_falls_back_to_db_when_memory_is_empty(self, ti_db):
        cr.append_tick("NIFTY", _t(0), 24500.0)
        cr.append_tick("NIFTY", _t(65), 24505.0)

        # Simulate a fresh process -- in-memory state gone, DB persists.
        cr._completed.clear()
        candles = cr.get_recent_candles("NIFTY", "1m")
        assert len(candles) == 1
        assert candles[0]["open"] == 24500.0

    def test_duplicate_persist_for_the_same_bucket_does_not_duplicate_rows(self, ti_db):
        candle = {"datetime": _t(0), "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 0}
        cr._persist_candle("NIFTY", "1m", candle)
        cr._persist_candle("NIFTY", "1m", candle)
        rows = cr._load_from_db("NIFTY", "1m", limit=10)
        assert len(rows) == 1


class TestFreshnessMetrics:
    def test_last_candle_time_and_lag(self, ti_db):
        cr.append_tick("NIFTY", _t(0), 100.0)
        cr.append_tick("NIFTY", _t(65), 100.0)

        last = cr.last_candle_time("NIFTY", "1m")
        assert last == _t(0).replace(second=0, microsecond=0)

        lag = cr.candle_lag_seconds("NIFTY", "1m", now=_t(125))
        assert lag == 125.0

    def test_no_data_reports_none_not_zero(self, ti_db):
        assert cr.last_candle_time("NOT_A_REAL_SYMBOL", "1m") is None
        assert cr.candle_lag_seconds("NOT_A_REAL_SYMBOL", "1m") is None
