import datetime as dt

from agents.trading_intelligence import multi_timeframe as mtf


class TestGetTimeframe:
    def test_native_3m_reads_the_real_archive(self, ti_db):
        result = mtf.get_timeframe("NIFTY", "3m")
        assert result["available"] is True
        assert len(result["candles"]) > 1000

    def test_1m_is_honestly_unavailable_with_no_recorded_ticks(self, ti_db):
        result = mtf.get_timeframe("NIFTY", "1m")
        assert result["available"] is False
        assert "no in-process 1m candles recorded" in result["reason"]

    def test_5m_is_honestly_unavailable_with_no_recorded_ticks(self, ti_db):
        result = mtf.get_timeframe("NIFTY", "5m")
        assert result["available"] is False
        assert "no in-process 5m candles recorded" in result["reason"]

    def test_1m_becomes_available_once_candle_recorder_has_a_bar(self, ti_db):
        from agents.trading_intelligence import candle_recorder as cr
        base = dt.datetime(2026, 8, 13, 9, 0, 0)
        cr.append_tick("NIFTY", base, 24500.0)
        cr.append_tick("NIFTY", base + dt.timedelta(seconds=65), 24510.0)

        result = mtf.get_timeframe("NIFTY", "1m")
        assert result["available"] is True
        assert len(result["candles"]) == 1
        assert result["candles"].iloc[0]["open"] == 24500.0

    def test_15m_resamples_cleanly_from_3m(self, ti_db):
        result = mtf.get_timeframe("NIFTY", "15m")
        assert result["available"] is True
        base = mtf.get_timeframe("NIFTY", "3m")
        # 15m should have roughly 1/5th the bars of 3m (not exact due to
        # session boundaries/gaps, but should be in a sane range).
        ratio = len(base["candles"]) / len(result["candles"])
        assert 4.0 <= ratio <= 6.0

    def test_30m_and_1h_and_daily_all_available(self, ti_db):
        for tf in ("30m", "1h", "daily"):
            result = mtf.get_timeframe("NIFTY", tf)
            assert result["available"] is True, f"{tf} should be available"

    def test_daily_bars_have_one_row_per_real_trading_day(self, ti_db):
        result = mtf.get_timeframe("NIFTY", "daily")
        assert result["available"] is True
        assert len(result["candles"]) > 200  # this archive spans over a year of trading days

    def test_unavailable_for_unknown_symbol(self, ti_db):
        result = mtf.get_timeframe("NOT_A_REAL_SYMBOL", "15m")
        assert result["available"] is False

    def test_ohlc_aggregation_is_correct_for_one_resampled_bar(self, ti_db):
        """15m bar's high must be the MAX of its five constituent 3m
        bars' highs, low the MIN, open the FIRST, close the LAST -- real
        aggregation, not a placeholder."""
        base = mtf.get_timeframe("NIFTY", "3m")["candles"]
        resampled = mtf.get_timeframe("NIFTY", "15m")["candles"]
        first_bucket_start = resampled.iloc[0]["datetime"]
        window = base[(base["datetime"] >= first_bucket_start) &
                       (base["datetime"] < first_bucket_start + dt.timedelta(minutes=15))]
        assert resampled.iloc[0]["high"] == window["high"].max()
        assert resampled.iloc[0]["low"] == window["low"].min()
        assert resampled.iloc[0]["open"] == window.iloc[0]["open"]
        assert resampled.iloc[0]["close"] == window.iloc[-1]["close"]


class TestSynchronize:
    def test_returns_every_requested_timeframe(self, ti_db):
        result = mtf.synchronize("NIFTY")
        assert set(result.keys()) == set(mtf.ALL_REQUESTED_TIMEFRAMES)

    def test_mix_of_available_and_unavailable(self, ti_db):
        result = mtf.synchronize("NIFTY")
        assert result["1m"]["available"] is False
        assert result["3m"]["available"] is True
        assert result["daily"]["available"] is True
