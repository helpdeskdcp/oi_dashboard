"""
agents/runtime/market_session.py -- "supports market sessions."

Deliberately INDEPENDENT of app.py's own is_market_open()/MARKET_HOURS
(app.py:216) -- this codebase has never imported app.py from agents/,
and for the same reason every other agent module gives for it: app.py
is a ~7000-line Flask app with real broker-session machinery, and
importing it in a scheduler process (which IS meant to run continuously,
unlike a short-lived test) would be an even larger, more persistent
version of the same risk the /live-positions test landmine already
proved is real. This module re-derives the same NSE Equity F&O
trading-hours rule (09:15-15:40 IST, Mon-Fri -- updated 2026-08-09 for
the exchange's 2026-08-03 timing revision, extended from the prior
15:30 close) independently, in plain stdlib datetime -- no dependency
on app.py, no live broker call, ever. Keep this in sync by hand with
app.py's own MARKET_HOURS["index_option"] entry if that ever changes
again.

Milestone 19+: Commodity (MCX) hours ARE now modeled here (is_mcx_session_open()
below) -- TI_WATCHED_SYMBOLS mixes NSE indexes and MCX commodities, and
gating the whole engine on NSE-only hours meant it never ran at all
during MCX-only evening sessions. The actual close-TIME values still
come from mcx_session_config.py (the single source of truth app.py's
own _mcx_nonagri_close() also reads) -- not re-guessed here. Only the
DST-cutover-DATE arithmetic is duplicated from app.py's own
_mcx_nonagri_close(), for the same reason NSE hours were already
independently re-derived above: agents/ modules must never import
app.py. Keep the two copies of that date arithmetic in sync by hand if
either ever changes.
"""
import datetime as dt

import mcx_session_config
# Milestone 25: now_ist()/IST_OFFSET moved to agents/timekeeping.py as the
# single canonical implementation -- re-exported here unchanged so every
# existing `market_session.now_ist()` call site keeps working as-is.
from ..timekeeping import IST_OFFSET, now_ist  # noqa: F401

NSE_OPEN = (9, 15)
NSE_CLOSE = (15, 40)   # Equity F&O close, effective 2026-08-03 (was 15:30)
MCX_OPEN = (9, 0)

# Milestone 19+: which real-world exchange each TI_WATCHED_SYMBOLS member
# trades on -- the one place this mapping lives; agents/config.py's
# TI_WATCHED_SYMBOLS and app.py's own SYMBOLS dict both independently
# already encode "NSE Index" vs "MCX Commodity" per symbol (app.py's
# SYMBOLS[...]["group"]), but importing either from here would repeat
# the same app.py-import problem NSE hours already avoid above, or
# create a agents/config.py <-> agents/runtime/market_session.py
# ordering dependency for no real benefit -- this is a small, static,
# rarely-changing fact table, safe to keep here directly.
EXCHANGE_MAP = {
    "NIFTY": "NSE", "BANKNIFTY": "NSE", "SENSEX": "NSE",
    "NATURALGAS": "MCX", "NATGASMINI": "MCX",
    "CRUDEOIL": "MCX", "CRUDEOILM": "MCX",
    "GOLD": "MCX", "GOLDM": "MCX",
    "SILVER": "MCX", "SILVERM": "MCX",
}


def _nth_weekday_of_month(year: int, month: int, weekday: int, n: int) -> dt.datetime:
    """The n-th occurrence of `weekday` (0=Monday..6=Sunday) in
    `month`/`year`. Duplicated verbatim from app.py's own helper of the
    same name -- see module docstring."""
    d = dt.date(year, month, 1)
    days_ahead = (weekday - d.weekday()) % 7
    d += dt.timedelta(days=days_ahead + 7 * (n - 1))
    return dt.datetime(d.year, d.month, d.day)


def _mcx_nonagri_close(now: dt.datetime) -> tuple:
    """(hour, minute) for MCX's non-agricultural (metals/energy/bullion --
    every MCX symbol in EXCHANGE_MAP above) session close, which shifts
    seasonally. Mirrors app.py's own _mcx_nonagri_close() exactly (same
    APPROXIMATE US-DST-calendar cutover window; see
    mcx_session_config.MCX_DST_MODE for the same caveat) -- the actual
    close-time VALUES always come from mcx_session_config.py, never
    duplicated here, only the cutover-date arithmetic is."""
    year = now.year
    dst_start = _nth_weekday_of_month(year, 3, 6, 2)    # 2nd Sunday of March
    dst_end = _nth_weekday_of_month(year, 11, 6, 1)     # 1st Sunday of November
    if dst_start.date() <= now.date() < dst_end.date():
        return mcx_session_config.summer_close()
    return mcx_session_config.winter_close()


def is_nse_session_open(*, at: dt.datetime | None = None) -> tuple:
    """Returns (open: bool, reason: str). reason is "" when open, else
    why not ("Weekend" / "Outside trading hours") -- same shape as
    app.py's is_market_open() for a human reading both side by side,
    without sharing any code path with it."""
    now = at or now_ist()
    if now.weekday() >= 5:
        return False, "Weekend"
    open_t = now.replace(hour=NSE_OPEN[0], minute=NSE_OPEN[1], second=0, microsecond=0)
    close_t = now.replace(hour=NSE_CLOSE[0], minute=NSE_CLOSE[1], second=0, microsecond=0)
    if open_t <= now <= close_t:
        return True, ""
    return False, "Outside trading hours"


def is_mcx_session_open(*, at: dt.datetime | None = None) -> tuple:
    """Same (open: bool, reason: str) contract as is_nse_session_open()
    above. Covers every EXCHANGE_MAP "MCX" symbol -- all non-agricultural
    (metals/energy/bullion) today, so MCX_OPEN + _mcx_nonagri_close() is
    the correct single window for all of them; would need a
    per-symbol-type split here (like app.py's own commodity_agri vs
    commodity_nonagri) if an agri commodity is ever added to
    EXCHANGE_MAP."""
    now = at or now_ist()
    if now.weekday() >= 5:
        return False, "Weekend"
    oh, om = MCX_OPEN
    ch, cm = _mcx_nonagri_close(now)
    open_t = now.replace(hour=oh, minute=om, second=0, microsecond=0)
    close_t = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
    if open_t <= now <= close_t:
        return True, ""
    return False, "Outside MCX trading hours"


def is_exchange_open(exchange: str, *, at: dt.datetime | None = None) -> tuple:
    """Dispatches to the right session check by exchange name -- an
    unrecognized exchange is honestly reported closed with a reason,
    never silently treated as open."""
    if exchange == "NSE":
        return is_nse_session_open(at=at)
    if exchange == "MCX":
        return is_mcx_session_open(at=at)
    return False, f"unknown exchange {exchange!r}"


def active_symbols(symbols, *, at: dt.datetime | None = None) -> list:
    """Filters `symbols` down to the ones whose exchange (via
    EXCHANGE_MAP, default NSE for an unmapped symbol) is open right now
    -- e.g. only the MCX names when NSE is closed but MCX is still
    trading. Order-preserving."""
    return [s for s in symbols if is_exchange_open(EXCHANGE_MAP.get(s, "NSE"), at=at)[0]]


def any_watched_exchange_open(symbols, *, at: dt.datetime | None = None) -> bool:
    """True as soon as ANY of `symbols`' exchanges is open -- what a
    scheduler gate should check before running a cycle over a symbol
    list that spans multiple exchanges, instead of gating the whole
    cycle on one exchange's hours alone."""
    return any(is_exchange_open(EXCHANGE_MAP.get(s, "NSE"), at=at)[0] for s in symbols)


def seconds_until_next_open(*, at: dt.datetime | None = None) -> float:
    """How long the scheduler should sleep before checking again when
    the market is closed -- avoids a busy-poll loop burning CPU every
    tick while waiting out a weekend or an overnight gap."""
    now = at or now_ist()
    candidate = now.replace(hour=NSE_OPEN[0], minute=NSE_OPEN[1], second=0, microsecond=0)
    if now >= candidate:
        candidate += dt.timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += dt.timedelta(days=1)
    return (candidate - now).total_seconds()
