"""agents/timekeeping.py -- Milestone 25 Workstream 1: the one canonical
time contract for every trading-domain timestamp in this application.

POLICY (read this before touching any datetime call site):

Every PERSISTED or cross-module-COMPARED timestamp in the trading/paper-
trading/analytics path is naive IST wall-clock time -- a `datetime` with
no tzinfo, whose year/month/day/hour/minute/second already read as
India Standard Time. This is a deliberate choice, not an oversight:

  1. It is already the dominant, working convention. app.py's own
     now_ist() (used by 3 of the 4 paper-trading engines directly) and
     agents/runtime/market_session.py's now_ist() (the NSE/MCX session-
     hours engine) both already compute exactly this value, independently,
     byte-identically. This module gives both a single shared source
     instead of two hand-synchronized copies.
  2. India has one fixed UTC+05:30 offset, year-round, no DST -- unlike a
     US/EU deployment, "prefer UTC for storage, convert for display" buys
     no DST-safety benefit here, while a wholesale UTC-storage migration
     would touch every historical row and every comparison site in the
     app for a purely cosmetic gain.
  3. agents/ must never import app.py (the AST safety scan enforces this
     -- see test_agents/trading_intelligence/test_safety.py and every
     agents/runtime module's own docstring on the subject). A tz-aware
     zoneinfo/pytz dependency would not change that constraint either
     way, so it isn't the deciding factor -- consistency with the
     already-dominant convention is.

The actual, already-shipped bug this fixes is NOT "wrong timezone" --
it's "inconsistent, implicit representation". Before this module existed,
agents/trading_intelligence/ti_store.py and virtual_trailing.py each had
their own private `_now()` returning plain `dt.datetime.now().isoformat()`
-- correct IST wall-clock time ONLY because the writing server's OS
timezone happens to be configured as Asia/Kolkata, an assumption
performance_analytics.py's own M23 audit fix (_timezone_note()) already
had to work around defensively rather than fix at the source. Likewise
app.py's own run_symbol_loop() -- the single hot loop that drives every
live trading decision -- stamped candle ticks, cooldown windows, and the
Swing/S-R engine's entry_time_obj with raw dt.datetime.now() while the
Scalp/V3/Trading-Intelligence engines already used now_ist() a few
hundred lines away in the same file. Same clock in production today
(because the OS happens to be IST), but one OS-config change away from
silently desynchronizing trade timestamps from everything else -- exactly
the kind of landmine that already went off once (see
performance_analytics.py's own docstring on the market_structure_
snapshots.ts epoch-vs-ISO migration).

RULES FOR CALLERS:

- Never call dt.datetime.now() (or dt.datetime.utcnow()) for a value that
  gets persisted to a DB column, returned in an API response, or compared
  against another module's timestamp. Call now_ist() instead.
- Never mix a naive value from this module with a tz-aware datetime in a
  comparison -- Python raises TypeError immediately if you do, which is
  the correct, loud failure mode; do not silently strip or attach tzinfo
  to make the comparison "work". If you receive a tz-aware datetime from
  an external source, convert it explicitly with utc_to_ist() below, once,
  at the boundary where it enters this application.
- In-memory-only values that are never persisted, never returned to a
  caller, and never compared against a value from a different clock
  source (e.g. a function's own start/elapsed-time measurement using
  time.monotonic(), or a short-lived UI-commentary cache-staleness check
  that only ever compares against its own previous write) are explicitly
  OUT OF SCOPE for this migration -- converting those adds churn with no
  correctness benefit. When in doubt, check whether the value is ever
  compared against something written by a *different* function/module;
  if yes, it must use now_ist(); if it only ever compares against its own
  prior writes in the same function, either convention is safe, but
  now_ist() is still preferred for consistency going forward.
"""
import datetime as dt

IST_OFFSET = dt.timedelta(hours=5, minutes=30)
IST = dt.timezone(IST_OFFSET)


def now_ist() -> dt.datetime:
    """The canonical 'what time is it' for this application: naive IST
    wall-clock time, computed from the OS's own UTC clock (not the OS's
    configured local timezone) so the result is correct regardless of how
    the deployment host's TZ is set. This is the single implementation --
    agents/runtime/market_session.py and app.py both re-export this
    exact function rather than keeping their own copies."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + IST_OFFSET


def now_ist_iso() -> str:
    """now_ist() as the ISO string every trading-domain DB column and API
    response should use -- the single call every ti_store.py/
    virtual_trailing.py-style `_now()` helper should delegate to."""
    return now_ist().isoformat()


def utc_to_ist(value: dt.datetime) -> dt.datetime:
    """Converts a tz-aware datetime (any timezone) to this application's
    naive-IST convention. The one sanctioned place to turn an externally-
    sourced aware timestamp into something comparable against now_ist()
    output -- never do this conversion ad hoc at a comparison site.
    Raises ValueError on a naive input, since a naive datetime has no
    origin timezone to convert *from* -- silently assuming one would be
    exactly the "silently interpret a naive timestamp" mistake this
    module exists to prevent."""
    if value.tzinfo is None:
        raise ValueError(
            "utc_to_ist() requires a tz-aware datetime; a naive datetime has no "
            "recorded origin timezone to convert from. If this value is already "
            "IST wall-clock time, it needs no conversion -- use it as-is."
        )
    return value.astimezone(IST).replace(tzinfo=None)
