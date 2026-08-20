#!/usr/bin/env python3
"""
expiry_intelligence.py -- Milestone 17+: centralized expiry auto-detection
and expiry-day OI/scalping analytics.

Audit finding (before this module existed): expiry detection was NOT
missing from this codebase -- app.py's AngelOneFetcher already had a
mature, broker-instrument-master-driven implementation
(parse_expiry/is_expiry_today/find_nearest_expiry, now
list_available_expiries too), deliberately independent of NSE data so MCX
commodities (which have no NSE option-chain data) still resolve an expiry.
This module does NOT reimplement that scan. It wraps it: every date this
module returns is calendar-independent, sourced live from whatever
`fetcher.list_available_expiries(symbol)` reports for the currently loaded
instrument master -- never a hardcoded weekday/date rule -- so market
holidays shifting an expiry, or a monthly expiry silently replacing what
would normally be a weekly one, are picked up automatically the next time
the instrument master refreshes (app.py refreshes it if >24h stale).

Two independent halves:

1. Expiry resolution (get_expiry_status / get_all_index_expiry_flags) --
   needs a `fetcher` object exposing `list_available_expiries(symbol) ->
   list[date]` (AngelOneFetcher in app.py is the only implementation
   today; anything duck-typing the same method works, e.g. a test double).
   This module never imports app.py itself and never touches a broker
   session (self.client) -- only the fetcher's already-loaded, file-cached
   instrument list -- so it stays safely importable from
   intelligence_orchestrator.py, whose own module docstring documents a
   "never imports app.py" boundary (Milestone 13, Phase 1 constraints).

2. Expiry-day OI/scalping analytics (compute_scalping_metrics) -- pure,
   side-effect-free, takes an already-fetched option chain (the same
   `List[StrikeRow]` oi_engine.py's own functions take) plus the spot
   price/strike step/days-to-expiry, and returns ATM/OI-wall/unwinding/
   gamma-zone/theta-mode readings for that one cycle. Built ON TOP OF
   oi_engine.py's existing find_atm()/oi_walls() (not a duplicate) --
   oi_engine.py itself stays untouched: it's a pure function library with
   no hardcoded expiry logic to remove, and it must not gain a dependency
   on anything that fetches data (see oi_engine.py's own "never duplicate
   this logic elsewhere" docstring warning, which cuts both ways).

Advisory-only, same as every other analytics module in this codebase
(agents/trading_intelligence, institutional_intelligence's gamma-trap
findings): nothing here places an order, sizes a position, or is wired
into any execution path. EXPIRY_DAY_TRADE_PARAMS below is a set of
suggested risk-management multipliers for a human (or a future, separately
reviewed execution layer) to consider on expiry day -- computing it is not
the same as acting on it.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from oi_engine import find_atm, oi_walls

IST = ZoneInfo("Asia/Kolkata")

# Milestone 17+ spec's minimum required index set. BANKEX is deliberately
# excluded here -- confirmed via audit (grep of app.py's SYMBOLS dict) that
# no BANKEX entry exists in this dashboard's configured symbols as of this
# milestone, so it isn't "genuinely configured today". Pass an explicit
# `indexes` list to get_all_index_expiry_flags() to include it once/if BSE
# derivatives adds it.
DEFAULT_INDEXES = ("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "SENSEX")

# Milestone 17+ "Expiry Scalping Intelligence" spec's explicit expiry-day
# risk-management multipliers. Advisory data only -- see module docstring.
EXPIRY_DAY_TRADE_PARAMS = {
    "premium_target_multiplier": 1.5,
    "stoploss_multiplier": 0.7,
    "max_hold_minutes": 10,
    "use_only_atm_and_near_atm": True,
}


class ExpiryDataUnavailable(Exception):
    """Raised when a symbol has no expiry data in the current instrument
    master -- e.g. instrument master not loaded yet, or an unsupported/
    misspelled symbol. Callers that want a degraded-but-alive response
    (get_all_index_expiry_flags) catch this per-index rather than letting
    one bad symbol take down every other index's flags."""


def _today_ist() -> dt.date:
    return dt.datetime.now(IST).date()


def _classify_weekly_or_monthly(expiry: dt.date, all_dates) -> str:
    """MONTHLY iff `expiry` is the last expiry date within its own
    (year, month) among every date this symbol currently has listed --
    the real, calendar-independent definition (exchanges have shifted the
    monthly expiry weekday more than once; this never assumes which
    weekday it falls on). WEEKLY otherwise."""
    same_month = [d for d in all_dates if d.year == expiry.year and d.month == expiry.month]
    return "MONTHLY" if same_month and expiry == max(same_month) else "WEEKLY"


def load_available_expiries(index_name: str, fetcher) -> list:
    """Every distinct upcoming-or-current expiry date currently listed for
    `index_name`, chronologically sorted -- a thin pass-through to
    `fetcher.list_available_expiries()` (see module docstring for why this
    doesn't re-scan the instrument master itself)."""
    return list(fetcher.list_available_expiries(index_name))


def get_nearest_expiry(index_name: str, fetcher, today: dt.date = None) -> dt.date:
    """The single nearest expiry date (today or later -- never a past
    date, see get_expiry_status's docstring). Raises ExpiryDataUnavailable
    if the symbol has no expiry data at all, OR if every listed expiry is
    already in the past (a stale/unrefreshed instrument master) -- both
    are the same "cannot honestly resolve a current contract" case, and
    callers already handle this exception (see app.py's
    AngelOneFetcher.find_nearest_expiry() and _resolve_ti_expiry_dates(),
    both of which degrade to None/excluded on this exception)."""
    return get_expiry_status(index_name, fetcher, today=today)["next_expiry"]


def get_expiry_status(index_name: str, fetcher, today: dt.date = None, exchange: str = None) -> dict:
    """
    {"index", "next_expiry", "days_to_expiry", "expiry_today",
    "weekly_or_monthly", "is_weekly", "is_monthly", "source"} for one
    index, live-derived from the fetcher's currently loaded instrument
    master -- no weekday ever assumed (no "NIFTY=Thursday"-style table
    anywhere in this module); `is_monthly` is always computed the same
    way regardless of which weekday it happens to land on this cycle, so
    an exchange circular moving the monthly expiry's weekday is picked up
    automatically the next time the instrument master refreshes.

    `is_weekly`/`is_monthly` carry the exact same information as
    `weekly_or_monthly` (kept for the original spec's field name) --
    provided as booleans too since a later spec revision asked for both.

    `exchange`: optional caller-supplied label (e.g. app.py's
    SYMBOLS[symbol]["exch"], "NSE" or "BSE") echoed back verbatim for
    "which exchange resolved this" -- this module itself never inspects
    exchange segment, since `fetcher.list_available_expiries()` already
    matches by the correct per-symbol instrument rows regardless of
    exchange (SENSEX/BANKEX vs NIFTY/BANKNIFTY are distinct symbol
    names in the instrument master either way).

    Edge cases:
    - Empty expiry list (instrument master not loaded, or genuinely no
      contracts for this symbol): raises ExpiryDataUnavailable rather than
      guessing -- never fabricates a date.
    - Market holiday shifted the expiry, or a monthly expiry lands on what
      would otherwise be a weekly slot: both are transparent here, since
      `dates` always reflects whatever the broker's instrument master
      currently lists -- there is no separate holiday table to go stale.
    - If every listed date is somehow already in the past (a stale/
      unrefreshed instrument master), this ALSO raises ExpiryDataUnavailable
      rather than degrading to the most-recent past date. Fixed 2026-08-20
      (Codex review finding, HIGH): the previous degraded-fallback
      behavior violated the "never select an expired contract as the
      active expiry" invariant this whole module exists to enforce --
      a past `next_expiry` silently fed a negative days_to_expiry into
      every downstream caller (Black-Scholes time-to-expiry, expiry-day
      weighting, etc.), which is nonsensical, not "degraded but usable."
      Failing closed here is consistent with every real call site already
      catching ExpiryDataUnavailable and handling "no current expiry" as
      an honest unavailable state (None / excluded), never a guess.
    """
    today = today or _today_ist()
    # Defensively sorted here rather than trusting the fetcher's own
    # ordering contract -- AngelOneFetcher.list_available_expiries()
    # already returns chronological order, but nothing downstream should
    # break if a future/test fetcher doesn't.
    dates = sorted(load_available_expiries(index_name, fetcher))
    if not dates:
        raise ExpiryDataUnavailable(
            f"No expiry data available for {index_name!r} -- instrument master "
            "not loaded yet, or this symbol has no listed option/future contracts."
        )
    upcoming = [d for d in dates if d >= today]
    if not upcoming:
        raise ExpiryDataUnavailable(
            f"Every listed expiry for {index_name!r} is already in the past "
            f"(most recent: {dates[-1]}, today: {today}) -- instrument master "
            "appears stale/unrefreshed. Refusing to select an expired contract "
            "as the active expiry."
        )
    next_expiry = upcoming[0]
    days_to_expiry = (next_expiry - today).days
    weekly_or_monthly = _classify_weekly_or_monthly(next_expiry, dates)
    return {
        "index": index_name,
        "next_expiry": next_expiry,
        "days_to_expiry": days_to_expiry,
        "expiry_today": days_to_expiry == 0,
        "weekly_or_monthly": weekly_or_monthly,
        "is_weekly": weekly_or_monthly == "WEEKLY",
        "is_monthly": weekly_or_monthly == "MONTHLY",
        "source": "angelone_instrument_master",
        "exchange": exchange,
    }


def get_all_index_expiry_flags(fetcher, indexes=None, today: dt.date = None) -> dict:
    """get_expiry_status() for every index in `indexes` (default:
    DEFAULT_INDEXES; either a plain iterable of names, or a dict of
    {name: exchange_label} to also echo each index's exchange through --
    see get_expiry_status's own `exchange` param). A symbol with no
    expiry data degrades to {"index": ..., "error": "..."} in its own
    slot rather than raising -- one missing/misconfigured symbol must
    never blank out every other index's flags."""
    indexes = indexes if indexes is not None else DEFAULT_INDEXES
    items = indexes.items() if isinstance(indexes, dict) else ((idx, None) for idx in indexes)
    result = {}
    for idx, exchange in items:
        try:
            result[idx] = get_expiry_status(idx, fetcher, today=today, exchange=exchange)
        except ExpiryDataUnavailable as exc:
            result[idx] = {"index": idx, "error": str(exc)}
    return result


def global_context_from_flags(flags: dict) -> dict:
    """The pure reduction half of get_global_expiry_context() below,
    split out so a caller that already has a `flags` dict (e.g. a route
    that also needs the per-index detail) can derive both from ONE scan
    instead of two.

    - today_expiry_indexes: indexes whose next_expiry is today.
    - tomorrow_expiry_indexes: indexes whose next_expiry is exactly 1 day out.
    - monthly_expiry_week: True if any index's next_expiry is a MONTHLY
      expiry landing within the next 7 days -- derived purely from each
      index's own already-computed is_monthly/days_to_expiry, never a
      fixed "last week of the month" calendar rule.
    - high_gamma_day: True whenever any index expires today (gamma risk
      is structurally elevated market-wide on any expiry day, not just
      for that one index -- see expiry_intelligence.py's own
      _gamma_zone_activity() for the per-index reasoning this mirrors).
    """
    valid = [f for f in flags.values() if "error" not in f]
    today_expiry = [f["index"] for f in valid if f["expiry_today"]]
    tomorrow_expiry = [f["index"] for f in valid if f["days_to_expiry"] == 1]
    monthly_week = any(f["is_monthly"] and 0 <= f["days_to_expiry"] <= 7 for f in valid)
    return {
        "today_expiry_indexes": today_expiry,
        "tomorrow_expiry_indexes": tomorrow_expiry,
        "monthly_expiry_week": monthly_week,
        "high_gamma_day": len(today_expiry) > 0,
    }


def get_global_expiry_context(fetcher, indexes=None, today: dt.date = None) -> dict:
    """get_all_index_expiry_flags() + global_context_from_flags() in one
    call -- see the latter's docstring for what each field means. Use
    global_context_from_flags() directly instead when the caller already
    has a `flags` dict, to avoid scanning twice."""
    flags = get_all_index_expiry_flags(fetcher, indexes=indexes, today=today)
    return global_context_from_flags(flags)


def _oi_shift_velocity(atm_row) -> int:
    """0-3: today's |OI change| at the ATM strike as a PERCENTAGE of that
    side's own prior-cycle OI base -- the same percentage-based scaling
    oi_engine.classify_buildup()'s own min_oi_chg_pct mode uses, so a thin
    MCX contract and NIFTY are judged on the same relative scale rather
    than a single absolute contract-count threshold."""
    if not atm_row:
        return 0
    pct = 0.0
    ce_base = atm_row.ce_oi - atm_row.ce_oi_chg
    if ce_base > 0:
        pct = max(pct, abs(atm_row.ce_oi_chg) / ce_base * 100)
    pe_base = atm_row.pe_oi - atm_row.pe_oi_chg
    if pe_base > 0:
        pct = max(pct, abs(atm_row.pe_oi_chg) / pe_base * 100)
    if pct >= 20:
        return 3
    if pct >= 10:
        return 2
    if pct >= 5:
        return 1
    return 0


def _atm_volume_spike(atm_row, avg_atm_volume) -> int:
    """0-3: this cycle's combined ATM CE+PE volume vs a caller-supplied
    baseline average. Deliberately requires the caller to actually pass a
    real baseline (no built-in guess) -- with none available this scores 0
    rather than inventing a spike, matching this codebase's established
    "don't fabricate an answer" rule for missing history (see
    intelligence_history/shadow_mode's own pending/EXPIRED handling)."""
    if not atm_row or not avg_atm_volume or avg_atm_volume <= 0:
        return 0
    total_vol = (atm_row.ce_vol or 0) + (atm_row.pe_vol or 0)
    ratio = total_vol / avg_atm_volume
    if ratio >= 3:
        return 3
    if ratio >= 2:
        return 2
    if ratio >= 1.5:
        return 1
    return 0


def _gamma_zone_activity(gamma_risk_zone: bool, theta_decay_mode: bool) -> int:
    """0-3: gamma is structurally highest right at the ATM strike on
    expiry day itself (standard option theory -- delta swings fastest for
    an option about to expire right at the money), so this is scored from
    the two booleans already computed rather than re-deriving gamma from
    IV here (institutional_intelligence.gamma_trap_findings() already owns
    that heavier per-strike Black-Scholes gamma calculation; duplicating
    it here for a coarse 0-3 score isn't warranted)."""
    if gamma_risk_zone and theta_decay_mode:
        return 3
    if gamma_risk_zone:
        return 2
    return 0


def _infer_step(rows):
    """Strike spacing inferred directly from the option chain itself (the
    smallest positive gap between consecutive sorted strikes) -- lets
    callers that don't have the symbol's configured step handy (e.g.
    agents/trading_intelligence, which never imports app.py's SYMBOLS
    dict) still get a usable value, at the cost of needing >=2 strikes."""
    strikes = sorted({r.strike for r in rows})
    diffs = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    return min(diffs) if diffs else None


def compute_scalping_metrics(rows, underlying, days_to_expiry=None, atm=None, step=None,
                              avg_atm_volume=None) -> dict:
    """
    Pure, side-effect-free: one cycle's expiry-day OI/scalping reading for
    a single index, given its already-fetched option chain (`rows`, the
    same List[StrikeRow] oi_engine.py's own functions take) and spot price.

    `atm`/`step`: pass `atm` directly when the caller already has it (e.g.
    agents/trading_intelligence's MarketSnapshot.atm, persisted per-cycle
    by app.py -- recomputing it here would be a second, possibly-
    inconsistent computation of the same value). Otherwise pass `step`
    (app.py's SYMBOLS[symbol]["step"]) and ATM is derived via
    oi_engine.find_atm(). If neither is given, step is inferred from the
    strike spacing actually present in `rows`.

    Returns None if there isn't enough data yet to compute anything
    meaningful (no rows, no spot, no resolvable ATM) -- never a dict of
    fabricated zeros standing in for "unknown".
    """
    if not rows or not underlying:
        return None
    step = step or _infer_step(rows)
    if atm is None:
        if not step:
            return None
        atm = find_atm(underlying, step)

    support, resistance = oi_walls(rows)
    max_call_row = resistance[0] if resistance else None
    max_put_row = support[0] if support else None
    atm_row = next((r for r in rows if r.strike == atm), None)

    call_unwinding = bool(atm_row and atm_row.ce_signal == "Long Unwinding")
    put_unwinding = bool(atm_row and atm_row.pe_signal == "Long Unwinding")
    gamma_risk_zone = bool(step) and abs(underlying - atm) <= step  # "ATM +/- 1 strike"
    theta_decay_mode = days_to_expiry == 0

    oi_velocity = _oi_shift_velocity(atm_row)
    volume_spike = _atm_volume_spike(atm_row, avg_atm_volume)
    gamma_activity = _gamma_zone_activity(gamma_risk_zone, theta_decay_mode)
    pressure_score = (
        call_unwinding * 2 + put_unwinding * 2 + oi_velocity + volume_spike + gamma_activity
    )

    metrics = {
        "atm_strike": atm,
        "max_call_oi_strike": max_call_row.strike if max_call_row else None,
        "max_put_oi_strike": max_put_row.strike if max_put_row else None,
        "max_call_oi_change": max_call_row.ce_oi_chg if max_call_row else 0,
        "max_put_oi_change": max_put_row.pe_oi_chg if max_put_row else 0,
        "call_unwinding_detected": call_unwinding,
        "put_unwinding_detected": put_unwinding,
        "gamma_risk_zone": gamma_risk_zone,
        "theta_decay_mode": theta_decay_mode,
        "days_to_expiry": days_to_expiry,
        "expiry_pressure_score": pressure_score,
    }
    if theta_decay_mode:
        metrics["expiry_day_trade_params"] = dict(EXPIRY_DAY_TRADE_PARAMS)
    return metrics
