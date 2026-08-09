"""
history_engine.py -- Angel One Historical Data Engine

Downloads, stores, and incrementally updates historical OHLCV candle data
for tracked symbols across multiple timeframes, with chunked fetching
(large date-ranges split into manageable API calls), deduplication, gap
detection, and CSV/Parquet export.

Does NOT duplicate authentication -- reuses an already-logged-in
AngelOneFetcher instance (the same one app.py's live loop uses) for the
actual API calls and token/expiry resolution (instrument-master lookup,
nearest-active-MCX-future selection, etc. all already exist there).

USAGE (on VPS, in ~/oi_dashboard, with the venv activated):

    from app import AngelOneFetcher, SYMBOLS
    from history_engine import HistoryEngine

    angel = AngelOneFetcher()   # logs in using the same .env credentials as the live app
    engine = HistoryEngine(angel, SYMBOLS)

    # One-time backfill (Angel One provides ~1 year of history depending on timeframe)
    result = engine.fetch("NIFTY", "3m", "2025-07-23", "2026-07-23")
    print(result)

    # Daily incremental update -- only fetches candles since the last saved one
    result = engine.update_latest("NIFTY", "3m")

    # Validate for gaps/duplicates
    report = engine.verify_database("NIFTY", "3m")

    # Export paths (files are already written on every fetch/update -- this
    # just returns the path for convenience)
    path = engine.export("NIFTY", "3m", "csv")
"""
import os
import time
import logging
import datetime as dt
from pathlib import Path

import pandas as pd

log = logging.getLogger("history_engine")

_IST_OFFSET = dt.timedelta(hours=5, minutes=30)


def _now_ist():
    """UTC-safe IST 'now' -- plain dt.date.today() follows the server OS
    timezone; on a UTC-clocked VPS that's already tomorrow in IST after
    18:30 UTC, which would compute the wrong incremental-fetch window."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + _IST_OFFSET

try:
    import pyarrow   # noqa: F401 -- presence check only, for parquet support
    PARQUET_AVAILABLE = True
except ImportError:
    PARQUET_AVAILABLE = False

DATA_DIR = Path("data/history")
CHUNK_DAYS = 30   # Angel One's getCandleData has practical per-request limits; 30-day chunks are safely within them for intraday intervals

TIMEFRAMES = {
    "1m": "ONE_MINUTE", "3m": "THREE_MINUTE", "5m": "FIVE_MINUTE",
    "15m": "FIFTEEN_MINUTE", "30m": "THIRTY_MINUTE", "1h": "ONE_HOUR", "1d": "ONE_DAY",
}
INTERVAL_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "1d": 1440}

CANDLE_COLUMNS = ["datetime", "open", "high", "low", "close", "volume"]


class HistoryEngine:
    def __init__(self, angel_fetcher, symbols_config, data_dir=None):
        """
        angel_fetcher: an authenticated AngelOneFetcher instance (from app.py)
        symbols_config: the SYMBOLS dict from app.py (for type/exchange info)
        """
        self.angel = angel_fetcher
        self.symbols_config = symbols_config
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # -- storage paths --------------------------------------------------

    def _symbol_dir(self, symbol):
        d = self.data_dir / symbol
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _parquet_path(self, symbol, timeframe):
        return self._symbol_dir(symbol) / f"{timeframe}.parquet"

    def _csv_path(self, symbol, timeframe):
        return self._symbol_dir(symbol) / f"{timeframe}.csv"

    # -- load / save (dedup + sort always applied on save) --------------

    def _load_existing(self, symbol, timeframe):
        """Prefers parquet (faster, typed) but falls back to CSV if parquet
        support isn't installed or the parquet file doesn't exist yet."""
        pq_path = self._parquet_path(symbol, timeframe)
        csv_path = self._csv_path(symbol, timeframe)
        if PARQUET_AVAILABLE and pq_path.exists():
            df = pd.read_parquet(pq_path)
        elif csv_path.exists():
            df = pd.read_csv(csv_path, parse_dates=["datetime"])
        else:
            return pd.DataFrame(columns=CANDLE_COLUMNS)
        df["datetime"] = pd.to_datetime(df["datetime"])
        return df

    def _save(self, symbol, timeframe, df):
        if len(df) == 0:
            return df
        df = df.drop_duplicates(subset="datetime", keep="last").sort_values("datetime").reset_index(drop=True)
        df.to_csv(self._csv_path(symbol, timeframe), index=False)
        if PARQUET_AVAILABLE:
            df.to_parquet(self._parquet_path(symbol, timeframe), index=False)
        return df

    # -- token/exchange resolution (reuses app.py's existing logic) -----

    def _resolve_token(self, symbol):
        cfg = self.symbols_config.get(symbol)
        if not cfg:
            raise ValueError(f"Unknown symbol: {symbol}. Available: {list(self.symbols_config.keys())}")
        # Populate the token cache first (these calls have the side effect of
        # caching the resolved token -- same pattern the live app relies on).
        if cfg["type"] in ("commodity_agri", "commodity_nonagri"):   # mirrors app.py's own COMMODITY_TYPES
            self.angel.get_commodity_underlying(symbol)   # auto-detects nearest active FUTCOM expiry
        else:
            self.angel.get_index_spot_ltp(symbol)
        token, exchange = self.angel.get_underlying_token_for_candles(symbol, cfg)
        if not token:
            raise RuntimeError(f"Could not resolve a token for {symbol} -- check instrument master / Angel One session.")
        return token, exchange

    # -- fetching with retry + exponential backoff -----------------------

    def _fetch_chunk_with_retry(self, token, exchange, interval, from_dt, to_dt, max_retries=3):
        """Returns the candle list. Raises RuntimeError if every attempt
        errored out -- callers must NOT treat that the same as a legitimate
        empty result (holiday/no-trading-day), which get_historical_candles_range
        returns as [] without raising."""
        last_exc = None
        for attempt in range(max_retries):
            try:
                return self.angel.get_historical_candles_range(token, exchange, interval, from_dt, to_dt)
            except Exception as e:
                last_exc = e
                if attempt < max_retries - 1:
                    wait = 2 ** attempt   # exponential backoff: 1s, 2s, 4s
                    time.sleep(wait)
        raise RuntimeError(
            f"Historical candle fetch failed after {max_retries} attempts "
            f"for {from_dt}..{to_dt}: {last_exc}"
        ) from last_exc

    # -- public API (per spec) -------------------------------------------

    def fetch(self, symbol, timeframe, from_date, to_date):
        """
        Fetch historical candles for [from_date, to_date] (YYYY-MM-DD
        strings), chunked into CHUNK_DAYS-day pieces, merged with any
        existing saved data, deduplicated, sorted, and saved to disk.
        """
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"Unknown timeframe '{timeframe}'. Use one of {list(TIMEFRAMES)}")
        interval = TIMEFRAMES[timeframe]
        token, exchange = self._resolve_token(symbol)

        from_dt = dt.datetime.strptime(from_date, "%Y-%m-%d")
        to_dt = dt.datetime.strptime(to_date, "%Y-%m-%d") + dt.timedelta(hours=23, minutes=59)

        t0 = time.time()
        all_new_candles = []
        failed_chunks = []
        chunk_start = from_dt
        chunk_count = 0
        while chunk_start <= to_dt:
            chunk_end = min(chunk_start + dt.timedelta(days=CHUNK_DAYS), to_dt)
            chunk_count += 1
            try:
                candles = self._fetch_chunk_with_retry(token, exchange, interval, chunk_start, chunk_end)
                all_new_candles.extend(candles)
            except Exception as e:
                log.warning(f"{symbol}/{timeframe}: chunk {chunk_start}..{chunk_end} failed, skipping: {e}")
                failed_chunks.append({"from": str(chunk_start), "to": str(chunk_end), "error": str(e)})
            chunk_start = chunk_end + dt.timedelta(minutes=INTERVAL_MINUTES[timeframe])
            time.sleep(0.3)   # courtesy delay between chunks -- rate-limit-aware

        new_df = pd.DataFrame(all_new_candles, columns=CANDLE_COLUMNS) if all_new_candles else pd.DataFrame(columns=CANDLE_COLUMNS)
        existing_df = self._load_existing(symbol, timeframe)
        before_count = len(existing_df)
        combined = pd.concat([existing_df, new_df], ignore_index=True)
        saved = self._save(symbol, timeframe, combined)

        elapsed = round(time.time() - t0, 2)
        return {
            "symbol": symbol, "timeframe": timeframe,
            "chunks_fetched": chunk_count,
            "chunks_failed": len(failed_chunks),
            "failed_chunks": failed_chunks,   # non-empty means this run is INCOMPLETE, not just "0 new candles"
            "candles_fetched_this_run": len(new_df),
            "total_candles_after_merge": len(saved),
            "net_new_candles": len(saved) - before_count,
            "first_candle": str(saved["datetime"].min()) if len(saved) else None,
            "last_candle": str(saved["datetime"].max()) if len(saved) else None,
            "download_time_seconds": elapsed,
        }

    def update_latest(self, symbol, timeframe="3m"):
        """
        Incremental daily update: fetches ONLY the candles since the last
        saved one (never re-downloads existing history). Call fetch() first
        to establish an initial baseline if no history exists yet.
        """
        existing_df = self._load_existing(symbol, timeframe)
        if len(existing_df) == 0:
            return {"symbol": symbol, "timeframe": timeframe, "error":
                     "No existing history -- call fetch() first to establish a baseline."}
        last_dt = existing_df["datetime"].max()
        from_date = (last_dt + dt.timedelta(minutes=INTERVAL_MINUTES[timeframe])).strftime("%Y-%m-%d")
        to_date = _now_ist().strftime("%Y-%m-%d")
        if from_date > to_date:
            return {"symbol": symbol, "timeframe": timeframe, "net_new_candles": 0, "message": "Already up to date."}
        result = self.fetch(symbol, timeframe, from_date, to_date)
        if result.get("chunks_failed"):
            # Distinguish a real outage from "already up to date" -- both would
            # otherwise show 0 net_new_candles and look identical to a caller.
            result["message"] = (
                f"INCOMPLETE: {result['chunks_failed']}/{result['chunks_fetched']} chunk(s) "
                f"failed after retries -- do not treat this as 'up to date'."
            )
        return result

    def verify_database(self, symbol, timeframe="3m"):
        """
        Checks the saved history for duplicate candles and estimates missing
        candles (intraday gaps larger than expected -- overnight/weekend
        gaps between different trading days are correctly NOT counted as
        'missing', since no candle should exist there anyway).
        """
        df = self._load_existing(symbol, timeframe)
        if len(df) == 0:
            return {"symbol": symbol, "timeframe": timeframe, "total_candles": 0,
                     "duplicate_count": 0, "missing_candle_estimate": 0}
        df = df.sort_values("datetime")
        dup_count = int(df.duplicated(subset="datetime").sum())

        gaps_minutes = df["datetime"].diff().dt.total_seconds() / 60
        same_trading_day = df["datetime"].dt.date == df["datetime"].shift().dt.date
        expected_gap = INTERVAL_MINUTES[timeframe]
        missing_mask = same_trading_day & (gaps_minutes > expected_gap * 1.5)
        missing_estimate = int(missing_mask.sum())

        return {
            "symbol": symbol, "timeframe": timeframe,
            "total_candles": len(df),
            "first_candle": str(df["datetime"].min()),
            "last_candle": str(df["datetime"].max()),
            "duplicate_count": dup_count,
            "missing_candle_estimate": missing_estimate,
        }

    def export(self, symbol, timeframe, fmt="csv"):
        """Returns the path to the saved file for this symbol/timeframe
        (files are written automatically on every fetch/update -- this is a
        convenience accessor, not a separate export step)."""
        if fmt == "csv":
            path = self._csv_path(symbol, timeframe)
        elif fmt == "parquet":
            if not PARQUET_AVAILABLE:
                raise RuntimeError("pyarrow is not installed -- run: pip install pyarrow --break-system-packages")
            path = self._parquet_path(symbol, timeframe)
        else:
            raise ValueError("fmt must be 'csv' or 'parquet'")
        if not path.exists():
            raise FileNotFoundError(f"No data saved yet for {symbol} {timeframe} -- call fetch() first.")
        return str(path)

    # -- convenience: backfill + verify across all timeframes for one symbol --

    def backfill_all_timeframes(self, symbol, from_date, to_date, timeframes=None):
        """Runs fetch() for every requested timeframe (default: all of them)
        for one symbol -- useful for a one-time full backfill."""
        timeframes = timeframes or list(TIMEFRAMES.keys())
        results = {}
        for tf in timeframes:
            print(f"Fetching {symbol} [{tf}] from {from_date} to {to_date}...")
            results[tf] = self.fetch(symbol, tf, from_date, to_date)
            print(f"  -> {results[tf]['net_new_candles']} new candles, "
                  f"{results[tf]['total_candles_after_merge']} total, "
                  f"{results[tf]['download_time_seconds']}s")
        return results
