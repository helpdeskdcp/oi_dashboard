"""
agents/risk_manager/risk_decision.py -- Milestone 25 Workstream 3: the
one explainable answer to "is a new paper-trade/advisory action allowed
right now?", composed from the Live Portfolio Risk Monitor's own already-
real math (portfolio_monitor.py, agents/config.py's RISK_* thresholds)
rather than a second, parallel risk-scoring scheme.

This module NEVER closes a position, cancels an order, or halts trading
-- same propose-only posture portfolio_monitor.py already documents (see
its own module docstring: "this module NEVER closes a position, cancels
an order, or halts trading"). What's new here is a single, explicit
`allowed: bool`. The M25 audit's finding was specific: every risk signal
already existed as an after-the-fact alert (portfolio_monitor.snapshot().
alerts), but nothing anywhere composed those into a yes/no answer usable
as an actual entry gate -- daily-loss protection and exposure limits were
real numbers nobody ever asked "have we crossed them yet, before this
next trade." This module is that composition, not a new calculation.
Advisory/paper-trade only throughout -- no broker call, no order API,
anywhere in this file.

Scope (read the M25 audit before extending this): wired into
agents.trading_intelligence.paper_trading.enter_from_recommendation()
only -- the Trading Intelligence engine's own single choke point for
turning a Recommendation into a real ti_paper_trades row. Swing/Scalp/V3
(app.py) keep their own existing per-symbol one-trade-at-a-time gates
untouched; wiring an account-level gate into those three too is
explicitly out of this milestone's scope ("M25 must not become a general
refactor") -- tracked as backlog instead, not silently skipped.

UNITS: daily_loss below combines two DIFFERENT stored representations
correctly rather than blindly summing them -- see
agents/risk_manager/data_access.py's own UNITS WARNING for exactly why
ti_paper_trades.points (already qty-scaled) cannot be added to the
legacy engines' raw points without first applying the same rupee
conversion those legacy engines' own P&L display already applies
(app.py's PAPER_TRADE_LOT_QTY, mirrored into agents/config.py so this
package can read it without importing app.py). The result is still an
ESTIMATE for the three legacy engines specifically, because none of them
persists a real per-symbol lot size -- qty_basis_note below says so
explicitly, every time it's true, rather than presenting the number as
more precise than it is.
"""
import os

from .. import config, timekeeping
from . import data_access, portfolio_monitor

RISK_STATE_NORMAL = "NORMAL"
RISK_STATE_WARNING = "WARNING"
RISK_STATE_LIMIT_REACHED = "LIMIT_REACHED"
RISK_STATE_BLOCKED = "BLOCKED"

# Fraction of a limit at which a still-ALLOWED trade gets a WARNING state
# instead of NORMAL -- a config-style constant (env-overridable, matching
# every RISK_* threshold's own convention in agents/config.py) rather than
# a bare magic number in the branch logic below.
WARNING_THRESHOLD_PCT = float(os.getenv("RISK_WARNING_THRESHOLD_PCT", "70.0"))


def _pct_of_limit(value: float, limit: float) -> float:
    if not limit:
        return 0.0
    return round(value / limit * 100, 1)


def evaluate_trade_permission(*, symbol: str | None = None) -> dict:
    """The one explainable "can a new paper-trade/advisory action happen
    right now" answer. Always portfolio-wide/system-level (user_id=None,
    matching portfolio_monitor.snapshot()'s own "no live user session"
    view) -- this gates the Trading Intelligence engine's own automated
    paper trading, which has no logged-in user or personal wallet, not
    a specific person's manual Manual-Trading positions.

    `symbol`, when given, additionally checks that symbol's own exposure
    against RISK_MAX_EXPOSURE_PER_SYMBOL_PCT; when omitted, checks total
    portfolio exposure against RISK_PORTFOLIO_HEAT_LIMIT_PCT instead --
    the same two limits portfolio_monitor.py's own alerts already use,
    reused here rather than duplicated."""
    positions = data_access.all_open_positions(user_id=None)
    capital = config.RISK_ACCOUNT_CAPITAL
    today = timekeeping.now_ist().date().isoformat()

    legacy_points = data_access.daily_realized_pnl(None, since_date=today)
    ti_rupees = data_access.ti_daily_realized_pnl(since_date=today)
    daily_pnl_rupees = legacy_points * config.PAPER_TRADE_LOT_QTY + ti_rupees
    daily_loss = round(max(0.0, -daily_pnl_rupees), 2)
    daily_loss_limit = round(capital * config.RISK_DAILY_LOSS_LIMIT_PCT / 100, 2)

    if symbol:
        scoped_positions = [p for p in positions if p.symbol == symbol]
        current_exposure = portfolio_monitor.compute_exposure(scoped_positions)
        exposure_limit = round(capital * config.RISK_MAX_EXPOSURE_PER_SYMBOL_PCT / 100, 2)
    else:
        current_exposure = portfolio_monitor.compute_exposure(positions)
        exposure_limit = round(capital * config.RISK_PORTFOLIO_HEAT_LIMIT_PCT / 100, 2)

    open_count = len(positions)
    daily_loss_pct = _pct_of_limit(daily_loss, daily_loss_limit)
    exposure_pct = _pct_of_limit(current_exposure, exposure_limit)
    worst_pct = max(daily_loss_pct, exposure_pct)

    if open_count >= config.RISK_MAX_CONCURRENT_STRATEGIES:
        risk_state, allowed, reason = (
            RISK_STATE_BLOCKED, False,
            (
                f"{open_count} open position(s) already at or above the {config.RISK_MAX_CONCURRENT_STRATEGIES}-"
                f"position concurrent limit (RISK_MAX_CONCURRENT_STRATEGIES) -- structurally blocked "
                f"independent of today's P&L."
            ),
        )
    elif daily_loss_pct >= 100:
        risk_state, allowed, reason = (
            RISK_STATE_LIMIT_REACHED, False,
            (
                f"today's realized loss (Rs {daily_loss:.2f}) has reached the daily loss limit "
                f"(Rs {daily_loss_limit:.2f}, {config.RISK_DAILY_LOSS_LIMIT_PCT}% of capital) -- "
                f"no new entries until the next trading day."
            ),
        )
    elif exposure_pct >= 100:
        scope = f"{symbol} " if symbol else "portfolio "
        risk_state, allowed, reason = (
            RISK_STATE_LIMIT_REACHED, False,
            (
                f"{scope}exposure (Rs {current_exposure:.2f}) has reached its limit (Rs {exposure_limit:.2f}) -- "
                f"no new entries here until existing exposure is reduced."
            ),
        )
    elif worst_pct >= WARNING_THRESHOLD_PCT:
        risk_state, allowed, reason = (
            RISK_STATE_WARNING, True,
            (
                f"within limits but at {worst_pct:.0f}% of the tighter of the daily-loss/exposure limit -- "
                f"a new entry is still allowed, headroom is shrinking."
            ),
        )
    else:
        risk_state, allowed, reason = (
            RISK_STATE_NORMAL, True, "within all configured daily-loss and exposure limits.",
        )

    estimated_count = sum(1 for p in positions if p.qty_is_estimated)
    qty_basis_note = None
    if estimated_count:
        qty_basis_note = (
            f"{estimated_count} of {open_count} open position(s) (Swing/Scalp/V3 engines) have no persisted "
            f"per-trade quantity -- exposure for those uses qty=1, and today's P&L for those engines is "
            f"estimated using the global PAPER_TRADE_LOT_QTY constant (currently {config.PAPER_TRADE_LOT_QTY}) "
            f"rather than each symbol's real exchange lot size. Trading Intelligence positions use their real "
            f"per-trade quantity throughout."
        )

    return {
        "allowed": allowed,
        "risk_state": risk_state,
        "reason": reason,
        "daily_loss": daily_loss,
        "daily_loss_limit": daily_loss_limit,
        "current_exposure": round(current_exposure, 2),
        "exposure_limit": exposure_limit,
        "remaining_capacity": round(
            max(0.0, min(daily_loss_limit - daily_loss, exposure_limit - current_exposure)), 2,
        ),
        "open_position_count": open_count,
        "symbol": symbol,
        "qty_basis_note": qty_basis_note,
        "timestamp": timekeeping.now_ist_iso(),
    }
