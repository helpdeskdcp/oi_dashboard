"""
agents/trading_intelligence/multi_timeframe.py -- Module 4: Multi
Timeframe Engine. "Synchronize: 1m, 3m, 5m, 15m, 30m, 1H, Daily."

REAL, IMPORTANT FINDING, stated plainly rather than worked around: this
repository has only ever archived 3-minute candles (`data/history/<symbol>/
3m.*`) -- confirmed by a direct filesystem check across every symbol
directory that exists. `history_engine.py` is technically CAPABLE of
fetching 1m/5m/15m/30m/1h/1d directly from Angel One (its own
TIMEFRAMES dict lists all of them), but has never actually done so; no
archive for any timeframe but 3m exists on disk. Given that:

- 15m, 30m, 1H, and Daily ARE genuinely derivable from the 3m archive by
  real local resampling (15m = 5 consecutive 3m bars, 30m = 10, 1H = 20,
  Daily = every bar in one calendar date) -- built here for real, using
  pandas' own resample aggregation (open=first, high=max, low=min,
  close=last, volume=sum), never fabricated bars.
- 1m CANNOT be derived from a 3m archive at all -- you cannot recover
  finer granularity than what was actually recorded. This module reports
  it as unavailable, honestly, rather than inventing sub-bars.
- 5m is also NOT a clean multiple of 3m (5/3 is not an integer -- unlike
  15/3=5, 30/3=10, 60/3=20), so resampling 3m bars into "5m" buckets would
  silently misrepresent bar boundaries (a bucket wouldn't correspond to
  any real, atomic 3-minute reading boundary). Also reported as
  unavailable rather than built on a shaky assumption.

Both gaps are ONLY closeable by a genuine historical fetch from Angel One
via history_engine.py -- explicitly out of scope for a market data ENGINE
that reads what's already archived (and would need the shared live broker
session -- see package __init__.py's own safety rule -- so it's not
something this module should trigger on its own anyway).
"""
import pandas as pd

from . import data_access

NATIVE_TIMEFRAME = "3m"
# clean multiples of the native 3m archive -- values are the pandas
# resample rule string (NOT the same spelling as the timeframe name:
# pandas 3.x requires "min" for minutes, lowercase "h" for hours --
# "15m"/"1H" both raise ValueError, a real thing this module's own tests
# caught before it ever shipped).
DERIVABLE_TIMEFRAMES = {"15m": "15min", "30m": "30min", "1h": "1h"}
UNAVAILABLE_TIMEFRAMES = {
    "1m": "finer than the native 3m archive -- cannot be recovered by resampling coarser data",
    "5m": "not a clean multiple of the native 3m archive (5 is not divisible by 3) -- "
          "resampling would misrepresent real bar boundaries",
}
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
        candles = data_access.load_candles(symbol, timeframe="3m")
        if candles.empty:
            return {"timeframe": timeframe, "available": False, "candles": None,
                     "reason": f"no 3m archive for {symbol}"}
        return {"timeframe": timeframe, "available": True, "candles": candles, "reason": None}

    if timeframe in UNAVAILABLE_TIMEFRAMES:
        return {"timeframe": timeframe, "available": False, "candles": None,
                 "reason": UNAVAILABLE_TIMEFRAMES[timeframe]}

    base = data_access.load_candles(symbol, timeframe="3m")
    if base.empty:
        return {"timeframe": timeframe, "available": False, "candles": None,
                 "reason": f"no 3m archive for {symbol} to resample from"}

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
