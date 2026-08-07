"""
agents/runtime/market_session.py -- "supports market sessions."

Deliberately INDEPENDENT of app.py's own is_market_open()/MARKET_HOURS
(app.py:216) -- this codebase has never imported app.py from agents/,
and for the same reason every other agent module gives for it: app.py
is a ~7000-line Flask app with real broker-session machinery, and
importing it in a scheduler process (which IS meant to run continuously,
unlike a short-lived test) would be an even larger, more persistent
version of the same risk the /live-positions test landmine already
proved is real. This module re-derives the same NSE index trading-hours
rule (09:15-15:30 IST, Mon-Fri) independently, in plain stdlib
datetime -- no dependency on app.py, no live broker call, ever.

Commodity (MCX) hours are intentionally NOT modeled here (app.py's own
comment on MARKET_HOURS flags them as "approximate, verify against
exchange circular" even in the authoritative copy) -- this module only
answers for NSE index instruments, and says so honestly rather than
guessing at MCX hours a second time.
"""
import datetime as dt

IST_OFFSET = dt.timedelta(hours=5, minutes=30)
NSE_OPEN = (9, 15)
NSE_CLOSE = (15, 30)


def now_ist() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + IST_OFFSET


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
