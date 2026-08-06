"""
agents/trading_supervisor/market_state.py -- "Monitor market state
(trend, range, volatility, expiry, event risk)."

Trend/range regime is read from backtest.load_market_structure_snapshots
-- the SAME point-in-time market-structure data (ADX/ATR/regime/VWAP/
swing levels) app.py's own live strategies already compute and save,
not a second, possibly-disagreeing classifier. Volatility comes from
INDIA VIX's own historical candle archive (agents.quant_researcher.
data_access.load_candles -- the established OHLCV loader, reused rather
than duplicated).

Expiry and event risk have NO existing data source in this repo (no
economic calendar, no per-symbol expiry-date table) -- both accept an
optional caller-supplied calendar and honestly report "unknown" when
none is given, the same pattern agents.quant_researcher.features.
expiry_flag already established rather than guessing.
"""
import dataclasses


@dataclasses.dataclass
class MarketState:
    symbol: str
    date: str
    trend_range: dict  # {"regime": str, "adx": float|None, "atr_14": float|None} -- regime "unknown" if nothing logged
    volatility: dict  # {"level": "low"|"normal"|"high"|"unknown", "vix": float|None, "percentile": float|None}
    expiry: dict  # {"status": "today"|"tomorrow"|"normal"|"unknown"}
    event: dict  # {"status": "high"|"normal"|"unknown"}

    @property
    def has_unknowns(self) -> bool:
        return (
            self.trend_range.get("regime") == "unknown"
            or self.volatility.get("level") == "unknown"
            or self.expiry.get("status") == "unknown"
            or self.event.get("status") == "unknown"
        )

    @property
    def is_elevated_uncertainty(self) -> bool:
        return self.volatility.get("level") == "high" or self.expiry.get("status") in ("today", "tomorrow") \
            or self.event.get("status") == "high"


def trend_range_regime(symbol: str, date: str) -> dict:
    """Reads the market-structure snapshot logged for `date` (a
    "YYYY-MM-DD" string) -- {"regime": "unknown", "adx": None, "atr_14": None}
    if nothing was logged for that symbol/date (a brand-new symbol, a
    date before this feature existed in the live app, or the read itself
    failing -- DB unreachable, table missing). A module whose job is
    reporting market state honestly must degrade to "unknown" on a data
    failure, not crash the whole supervision pipeline over it."""
    import backtest
    try:
        snapshots = backtest.load_market_structure_snapshots(symbol, date, date)
    except Exception:
        return {"regime": "unknown", "adx": None, "atr_14": None}
    snapshot = snapshots.get(date)
    if snapshot is None:
        return {"regime": "unknown", "adx": None, "atr_14": None}
    ms = snapshot.get("market_structure") or {}
    return {"regime": ms.get("regime") or "unknown", "adx": ms.get("adx"), "atr_14": ms.get("atr_14")}


def volatility_regime(*, lookback_days: int = 20) -> dict:
    """Current INDIA VIX level vs its own trailing lookback_days
    percentile -- >= 80th percentile -> "high", <= 20th -> "low",
    otherwise "normal". "unknown" if there's no local VIX candle
    archive at all."""
    from ..quant_researcher import data_access as qr_data_access

    try:
        candles = qr_data_access.load_candles("INDIA VIX")
    except Exception:
        return {"level": "unknown", "vix": None, "percentile": None}
    if candles is None or candles.empty:
        return {"level": "unknown", "vix": None, "percentile": None}

    daily_close = candles.set_index("datetime")["close"].resample("1D").last().dropna().tail(lookback_days + 1)
    if len(daily_close) < 2:
        return {"level": "unknown", "vix": None, "percentile": None}

    current = float(daily_close.iloc[-1])
    history = daily_close.iloc[:-1]
    percentile = float((history < current).sum() / len(history) * 100)
    level = "high" if percentile >= 80 else ("low" if percentile <= 20 else "normal")
    return {"level": level, "vix": round(current, 2), "percentile": round(percentile, 1)}


def expiry_risk(date: str, *, expiry_dates: set | None = None) -> dict:
    if not expiry_dates:
        return {"status": "unknown"}
    if date in expiry_dates:
        return {"status": "today"}
    import datetime as dt
    next_day = (dt.date.fromisoformat(date) + dt.timedelta(days=1)).isoformat()
    if next_day in expiry_dates:
        return {"status": "tomorrow"}
    return {"status": "normal"}


def event_risk(date: str, *, event_dates: set | None = None) -> dict:
    if not event_dates:
        return {"status": "unknown"}
    return {"status": "high" if date in event_dates else "normal"}


def assess(symbol: str, date: str, *, expiry_dates: set | None = None, event_dates: set | None = None) -> MarketState:
    return MarketState(
        symbol=symbol, date=date,
        trend_range=trend_range_regime(symbol, date),
        volatility=volatility_regime(),
        expiry=expiry_risk(date, expiry_dates=expiry_dates),
        event=event_risk(date, event_dates=event_dates),
    )
