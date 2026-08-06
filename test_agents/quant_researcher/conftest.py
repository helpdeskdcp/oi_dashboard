"""
test_agents/quant_researcher/conftest.py -- synthetic OHLCV candles and
option-chain "cycles" shared across agents/quant_researcher/ tests.
Deliberately never touches data/history/ or oi_history.db -- every test
in this package works from data built here, matching this repo's
established "never a real DB/file in a unit test" convention.
"""
import datetime as dt

import pandas as pd
import pytest

from test_agents.dev_agent.conftest import git, toy_repo  # noqa: F401 -- re-exported fixtures


def _make_candles(n=60, *, start="2026-05-04 09:15:00", freq_minutes=3, base=100.0, drift=0.0, amplitude=1.0):
    times = [pd.Timestamp(start) + dt.timedelta(minutes=freq_minutes * i) for i in range(n)]
    rows = []
    price = base
    for i, ts in enumerate(times):
        price += drift
        wiggle = amplitude * (1 if i % 2 == 0 else -1) * 0.3
        o = price
        c = price + wiggle
        h = max(o, c) + amplitude * 0.5
        l = min(o, c) - amplitude * 0.5
        rows.append({"datetime": ts, "open": o, "high": h, "low": l, "close": c, "volume": 1000 + i})
        price = c
    return pd.DataFrame(rows)


@pytest.fixture
def trending_candles():
    """60 bars, gently trending up -- 3 sessions' worth at 3m bars, close
    enough to a real intraday archive's shape for CPR/resample tests."""
    return _make_candles(n=180, start="2026-05-04 09:15:00", drift=0.4, amplitude=1.2)


@pytest.fixture
def flat_candles():
    return _make_candles(n=60, start="2026-05-04 09:15:00", drift=0.0, amplitude=0.5)


@pytest.fixture
def empty_candles():
    return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])


def _make_cycles(candles: pd.DataFrame, *, atm=100.0, step=50.0, n_strikes=3):
    cycles = []
    for _, row in candles.iterrows():
        strikes = []
        for k in range(-n_strikes, n_strikes + 1):
            strike = atm + k * step
            strikes.append({
                "strike": strike,
                "ce_oi": 1000 + abs(k) * 10, "ce_oi_chg": 50 - k * 5, "ce_ltp": max(0.5, 20 - k * 3),
                "ce_delta": max(0.05, 0.5 - k * 0.1), "ce_gamma": 0.02, "ce_iv": 18.0 + abs(k) * 0.2,
                "pe_oi": 1000 + abs(k) * 8, "pe_oi_chg": 40 + k * 5, "pe_ltp": max(0.5, 20 + k * 3),
                "pe_delta": max(0.05, 0.5 + k * 0.1), "pe_gamma": 0.02, "pe_iv": 18.0 + abs(k) * 0.2,
            })
        cycles.append({
            "ts": row["datetime"].isoformat(), "date": row["datetime"].strftime("%Y-%m-%d"),
            "underlying_ltp": row["close"], "atm": atm, "max_pain": atm - 25,
            "pcr": 1.05, "strikes": strikes,
        })
    return cycles


@pytest.fixture
def cycles_for(trending_candles):
    return _make_cycles(trending_candles)


@pytest.fixture
def make_candles():
    return _make_candles


@pytest.fixture
def make_cycles():
    return _make_cycles
