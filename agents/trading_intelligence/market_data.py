"""
agents/trading_intelligence/market_data.py -- Module 1: Market Data Engine.

"Angel One SmartAPI. Live NIFTY. Live BANKNIFTY. Live SENSEX. Live Option
Chain. OI. OI Change. PCR. IV. Greeks. VWAP. Volume. Historical storage."

Every one of these is already flowing through this repository's existing,
live, production data path: app.py's own AngelOneFetcher writes option-chain
cycles into `cycles`/`strikes` (Milestone-1-of-this-whole-project-era code,
long before any agent existed) and candle archives into
`data/history/<symbol>/3m.*` via history_engine.py. "Historical storage" is
therefore not a new requirement this module has to build -- it already
exists and already works; what this module adds is a single, reusable
snapshot shape aggregating everything Module 1 asks for out of that
already-stored data, for any symbol, at any moment -- without ever opening
a second broker session (see package __init__.py's own safety rule).

IV and Greeks: `strikes.ce_iv/pe_iv` come straight from Angel One's quote
feed (already stored per-strike); delta/gamma/theta/vega use the SAME
Black-Scholes calculation app.py's live loop and oi_engine.generate_signal()
both already use (greeks.black_scholes_greeks) -- computed here only when
the stored strikes.ce_delta/pe_delta etc. are the coerced-zero default
(meaning this cycle predates the Greeks-column migration, or Angel One's
feed didn't populate them) and a real IV + expiry are available to compute
from; otherwise honestly left at whatever was actually stored.
"""
import dataclasses
import datetime as dt

from . import data_access


@dataclasses.dataclass
class MarketSnapshot:
    symbol: str
    as_of_ts: str | None
    underlying_ltp: float | None
    atm: float | None
    pcr: float | None
    pcr_change: float | None
    max_pain: float | None
    bias: str | None
    total_ce_oi: int
    total_pe_oi: int
    total_ce_oi_change: int
    total_pe_oi_change: int
    vwap: float | None
    latest_candle: dict | None
    volume_today: int | None
    strikes: list
    available: bool
    reason: str | None = None


def fill_missing_greeks(rows: list, *, underlying: float | None, expiry_date: dt.date | None) -> list:
    """Reuses greeks.black_scholes_greeks (the SAME function
    oi_engine.generate_signal already calls) to fill in delta/gamma for any
    strike whose stored value is the coerced-zero default AND a usable
    IV + underlying + expiry are available -- never invents an IV, never
    overwrites a genuinely-stored (even if zero) Greek with a guess.

    Public (not module-private) because strike_intelligence.py's own
    per-strike Gamma/Delta Exposure fields (Milestone 10's Priority-3
    review) need the exact same fill, for the exact same reason -- one
    shared implementation, called from both places, rather than a second
    copy of this Black-Scholes-filling logic living in strike_intelligence.py.
    Idempotent: calling it twice on the same rows (e.g. once inside
    get_snapshot(), again defensively inside strike_intelligence.build_table()
    for a caller that built its own StrikeRow list) is a safe no-op the
    second time, since it only ever fills a value that's still at the
    coerced-zero default."""
    if not underlying or not expiry_date:
        return rows
    from greeks import black_scholes_greeks, time_to_expiry_years
    T = time_to_expiry_years(expiry_date)
    for r in rows:
        for side, iv_attr, delta_attr, gamma_attr in (
            ("CE", "ce_iv", "ce_delta", "ce_gamma"), ("PE", "pe_iv", "pe_delta", "pe_gamma"),
        ):
            iv = getattr(r, iv_attr)
            if getattr(r, delta_attr) == 0.0 and getattr(r, gamma_attr) == 0.0 and iv and iv > 0:
                g = black_scholes_greeks(underlying, r.strike, T, iv, side)
                if g.get("valid"):
                    setattr(r, delta_attr, g["delta"])
                    setattr(r, gamma_attr, g["gamma"])
    return rows


def get_snapshot(symbol: str, *, expiry_date: dt.date | None = None) -> MarketSnapshot:
    """The full Module 1 picture for one symbol, aggregated from
    already-stored data only. `available=False` (with `reason`) rather than
    raising when no cycle has ever been logged for this symbol -- the same
    honest-degradation contract every prior milestone's data-reading module
    already holds to."""
    latest = data_access.latest_cycle(symbol)
    if latest is None:
        return MarketSnapshot(
            symbol=symbol, as_of_ts=None, underlying_ltp=None, atm=None, pcr=None, pcr_change=None,
            max_pain=None, bias=None, total_ce_oi=0, total_pe_oi=0, total_ce_oi_change=0,
            total_pe_oi_change=0, vwap=None, latest_candle=None, volume_today=None, strikes=[],
            available=False, reason=f"no option-chain cycle has ever been logged for {symbol}",
        )

    cycle, rows = latest["cycle"], latest["rows"]
    rows = fill_missing_greeks(rows, underlying=cycle.get("underlying_ltp"), expiry_date=expiry_date)

    history = data_access.recent_cycles(symbol, limit=2)
    pcr_change = None
    if len(history) == 2 and history[0].get("pcr") is not None and history[1].get("pcr") is not None:
        pcr_change = round(history[0]["pcr"] - history[1]["pcr"], 4)

    candles = data_access.load_candles(symbol)
    latest_candle, volume_today, vwap = None, None, None
    if not candles.empty:
        last_row = candles.iloc[-1]
        latest_candle = {
            "datetime": last_row["datetime"], "open": last_row["open"], "high": last_row["high"],
            "low": last_row["low"], "close": last_row["close"], "volume": last_row.get("volume"),
        }
        today = last_row["datetime"].date()
        today_candles = candles[candles["datetime"].dt.date == today]
        if "volume" in candles.columns:
            volume_today = int(today_candles["volume"].fillna(0).sum())
        from market_structure import calc_vwap
        candle_dicts = today_candles.to_dict("records")
        vwap = calc_vwap(candle_dicts, today)

    return MarketSnapshot(
        symbol=symbol, as_of_ts=cycle.get("ts"), underlying_ltp=cycle.get("underlying_ltp"),
        atm=cycle.get("atm"), pcr=cycle.get("pcr"), pcr_change=pcr_change, max_pain=cycle.get("max_pain"),
        bias=cycle.get("bias"),
        total_ce_oi=sum(r.ce_oi for r in rows), total_pe_oi=sum(r.pe_oi for r in rows),
        total_ce_oi_change=sum(r.ce_oi_chg for r in rows), total_pe_oi_change=sum(r.pe_oi_chg for r in rows),
        vwap=vwap, latest_candle=latest_candle, volume_today=volume_today, strikes=rows, available=True,
    )
