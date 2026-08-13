"""
agents/trading_intelligence/multi_timeframe.py -- Module 4: Multi
Timeframe Engine. "Synchronize: 1m, 3m, 5m, 15m, 30m, 1H, Daily."

This repository has only ever ARCHIVED 3-minute candles
(`data/history/<symbol>/3m.*`, refreshed once a day by fetch_history.py's
cron) -- confirmed by a direct filesystem check across every symbol
directory that exists. `history_engine.py` is technically CAPABLE of
fetching 1m/5m/15m/30m/1h/1d directly from Angel One (its own
TIMEFRAMES dict lists all of them), but the daily cron has never
actually done so for anything but 3m. Given that:

- 15m, 30m, 1H, and Daily ARE genuinely derivable from the 3m base by
  real local resampling (15m = 5 consecutive 3m bars, 30m = 10, 1H = 20,
  Daily = every bar in one calendar date) -- built here for real, using
  pandas' own resample aggregation (open=first, high=max, low=min,
  close=last, volume=sum), never fabricated bars.
- 1m and 5m are NOT derivable from a 3m base by resampling (1m is finer
  than 3m; 5 is not a clean multiple of 3) -- Milestone 20, Phase 6's
  candle_recorder.py closes this gap for real instead, building genuine
  1m/5m bars in-process from the live app's own LTP ticks (zero new
  broker calls -- see that module's own docstring). Below the recorder's
  own MAX_CANDLES_IN_MEMORY/DB window, or before the live app has been
  running long enough to have recorded any bars yet, this is reported as
  unavailable, honestly, rather than inventing sub-bars.

3m/1m/5m all go through data_access.load_fresh_candles() (archive merged
with candle_recorder's live bars), so every timeframe this module serves
reflects TODAY's real intraday price action, not just what fetch_history.py's
once-daily cron happened to have captured as of 18:00 IST.
"""
import pandas as pd

from . import data_access

NATIVE_TIMEFRAME = "3m"
RECORDER_TIMEFRAMES = ("1m", "5m")   # built by candle_recorder.py -- see module docstring above
# clean multiples of the native 3m base -- values are the pandas
# resample rule string (NOT the same spelling as the timeframe name:
# pandas 3.x requires "min" for minutes, lowercase "h" for hours --
# "15m"/"1H" both raise ValueError, a real thing this module's own tests
# caught before it ever shipped).
DERIVABLE_TIMEFRAMES = {"15m": "15min", "30m": "30min", "1h": "1h"}
ALL_REQUESTED_TIMEFRAMES = ("1m", "3m", "5m", "15m", "30m", "1h", "daily")

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def _resample(candles: pd.DataFrame, rule: str) -> pd.DataFrame:
    df = candles.set_index("datetime")
    agg = {k: v for k, v in _AGG.items() if k in df.columns}
    out = df.resample(rule).agg(agg).dropna(subset=["open"])
    return out.reset_index()


def get_timeframe(symbol: str, timeframe: str) -> dict:
    """One timeframe for one symbol. Returns {"timeframe", "available",
    "candles": DataFrame|None, "reason": str|None} -- never raises, never
    fabricates a bar. `timeframe` in {"1m","3m","5m","15m","30m","1h","daily"}."""
    if timeframe == NATIVE_TIMEFRAME:
        candles = data_access.load_fresh_candles(symbol, timeframe="3m")
        if candles.empty:
            return {"timeframe": timeframe, "available": False, "candles": None,
                     "reason": f"no 3m data (archive or live) for {symbol}"}
        return {"timeframe": timeframe, "available": True, "candles": candles, "reason": None}

    if timeframe in RECORDER_TIMEFRAMES:
        candles = data_access.load_fresh_candles(symbol, timeframe=timeframe)
        if candles.empty:
            return {"timeframe": timeframe, "available": False, "candles": None,
                     "reason": f"no in-process {timeframe} candles recorded yet for {symbol} "
                               f"(candle_recorder.py -- needs the live app running long enough "
                               f"to have closed at least one {timeframe} bucket)"}
        return {"timeframe": timeframe, "available": True, "candles": candles, "reason": None}

    base = data_access.load_fresh_candles(symbol, timeframe="3m")
    if base.empty:
        return {"timeframe": timeframe, "available": False, "candles": None,
                 "reason": f"no 3m data for {symbol} to resample from"}

    if timeframe == "daily":
        resampled = _resample(base, "1D")
    elif timeframe in DERIVABLE_TIMEFRAMES:
        resampled = _resample(base, DERIVABLE_TIMEFRAMES[timeframe])
    else:
        return {"timeframe": timeframe, "available": False, "candles": None,
                 "reason": f"unknown timeframe {timeframe!r} -- expected one of {ALL_REQUESTED_TIMEFRAMES}"}

    if resampled.empty:
        return {"timeframe": timeframe, "available": False, "candles": None,
                 "reason": "resample produced zero bars (unexpected -- check the source archive)"}
    return {"timeframe": timeframe, "available": True, "candles": resampled, "reason": None}


def synchronize(symbol: str) -> dict:
    """Every requested timeframe at once, keyed by name -- what the
    dashboard's multi-timeframe panel displays. Each value is the SAME
    shape get_timeframe() returns (available/reason/candles), so a
    partially-available result (e.g. only 3m/15m/30m/1h/daily, honestly
    missing 1m/5m) is never mistaken for an error."""
    return {tf: get_timeframe(symbol, tf) for tf in ALL_REQUESTED_TIMEFRAMES}
