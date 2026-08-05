#!/usr/bin/env python3
"""
position_sizing.py -- risk-based position sizing for Exit Engine V4's
backtest replay (backtest.py's simulate_dynamic_sr_v4_trades ->
exit_engine_v4.open_position). Backtest-only, same status as every other
V4 knob added so far -- see exit_engine_v4.py's module docstring ("no live
trading path exists for this engine today").

Two modes:
  "fixed"    -- every trade gets the same quantity (fixed_qty).
  "risk_pct" -- quantity is derived from how much of `capital` you're
                willing to risk (risk_pct% of it) divided by THIS
                SPECIFIC trade's actual per-unit risk (|entry -
                initial_sl|). Example from the spec: capital=100000,
                risk_pct=1 -> risk_amount=1000; a trade whose SL sits 50
                points from entry gets quantity=20 (1000/50).

Deliberately does NOT have separate "ATR-aware" or "structure-aware"
sizing modes alongside risk_pct -- the per-unit-risk denominator IS the
position's real initial_sl, which by the time open_position calls this is
already ATR-capped (see app.py's max_sl_atr_mult knob) and/or
structure-derived (dynamic_sr_engine.py's S/R ladder). A trade with a
wider ATR/structural stop automatically gets a smaller quantity for the
same capital risk, and vice versa -- that IS ATR-awareness and
structure-awareness, for free, with zero duplicate math. A second,
parallel "ATR-aware sizing formula" alongside this one would just
recompute the same entry-to-stop ratio a different way and risk the two
silently disagreeing about how much risk a trade actually carries.
"""

VALID_SIZING_MODES = (None, "fixed", "risk_pct")


def compute_quantity(entry, initial_sl, sizing_mode=None, capital=None, risk_pct=None,
                      fixed_qty=None, min_qty=None, max_qty=None):
    """Returns an integer quantity (>=0) for one trade.

    sizing_mode=None (default) preserves today's exact behavior: quantity
    is always 1, i.e. `points` remains the entire P&L signal for anyone
    not opting in -- backtest.py's `pnl_amount = points * quantity`
    degrades to `pnl_amount == points`.

    sizing_mode="fixed": quantity = fixed_qty (falls back to 1 if
    fixed_qty isn't given -- same as sizing_mode=None in that case).

    sizing_mode="risk_pct": quantity = floor(capital * risk_pct/100 /
    per_unit_risk), where per_unit_risk = abs(entry - initial_sl). Falls
    back to fixed_qty-or-1 if capital/risk_pct weren't both given, or if
    per_unit_risk is degenerate (<=0 -- shouldn't happen with a real
    signal's SL, but this must never divide by zero).

    min_qty/max_qty are OPTIONAL clamps applied after whichever mode
    computed a value (in every mode, not just risk_pct) -- neither
    implies a floor of 1 by itself. In particular, a risk_pct trade whose
    stop is too wide for the configured risk budget is allowed to size to
    0 (skip the trade) unless the caller explicitly sets min_qty to force
    a minimum -- silently overriding the stated risk budget just to force
    a trade to happen would defeat the point of risk-based sizing."""
    if sizing_mode not in VALID_SIZING_MODES:
        raise ValueError(f"Unknown sizing_mode {sizing_mode!r} -- expected one of {VALID_SIZING_MODES}.")

    if sizing_mode is None:
        qty = 1
    elif sizing_mode == "fixed":
        qty = fixed_qty if fixed_qty else 1
    else:   # "risk_pct"
        per_unit_risk = abs(entry - initial_sl)
        if not capital or not risk_pct or per_unit_risk <= 0:
            qty = fixed_qty if fixed_qty else 1
        else:
            qty = int((capital * (risk_pct / 100.0)) // per_unit_risk)

    if min_qty is not None:
        qty = max(qty, min_qty)
    if max_qty is not None:
        qty = min(qty, max_qty)
    return max(qty, 0)   # never negative regardless of how a bad clamp combination was configured
