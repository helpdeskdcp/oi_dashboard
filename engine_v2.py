"""
engine_v2.py -- parallel, independently-toggleable S/R engine.

Built 2026-07-24 from an empirical finding: across NIFTY (247 days),
BANKNIFTY (121 days), and NATURALGAS (128 days) of 6-month historical data,
the ORIGINAL formula (PDH+Range/2 for resistance, PDL-Range/2 for support)
was consistently biased -- price rarely reached that far. A much larger
Range-divisor (approaching plain PDH/PDL for NIFTY/BANKNIFTY; a
symbol-specific moderate divisor for NATURALGAS) was empirically far less
biased. See find_best_formula_coefficients.py for the search tool.

DESIGN GOALS (explicit, per request):
- Does NOT modify or touch Engine V1 (sr_probability_engine.py /
  market_structure.py's custom_range_levels) in any way.
- Runs in PARALLEL, reusing the SAME already-fetched live OI/PCR/volume
  data (no duplicate API calls) -- just different level-calculation.
- READ-ONLY / DISPLAY-ONLY for now: computes levels and probabilities,
  but does NOT place paper-trades or drive any live-trading decision.
  This is intentional -- the whole point of a separate, toggleable engine
  is to observe it before trusting it with real (paper-)trading.
- Per-symbol enable/disable, so each symbol's better-performing engine
  can be chosen independently once there's enough comparative data.

Reuses evaluate_resistance/evaluate_support from sr_probability_engine.py
directly (they accept level_price as a plain parameter) -- so ALL the
same OI/PCR/VWAP/regime/sweep evidence-scoring logic applies identically;
only the underlying level PRICES differ between V1 and V2.
"""
from sr_probability_engine import evaluate_resistance, evaluate_support

# Per-symbol divisor overrides, from the 2026-07-24 empirical search.
# Default (30) is a large-but-finite divisor, close to plain PDH/PDL without
# being a hardcoded special case -- matches NIFTY/BANKNIFTY's finding that
# bias kept shrinking all the way to the search boundary. NATURALGAS gets
# its own tuned value, since it showed a genuine (non-boundary) sweet spot.
V2_DIVISOR_BY_SYMBOL = {
    "NATURALGAS": 7.0,    # empirical sweet spot was ~5.5-8.75 depending on side; 7.0 splits the difference
    "NATGASMINI": 7.0,    # same underlying market as NATURALGAS
}
V2_DEFAULT_DIVISOR = 30.0


def compute_v2_levels(pdh: float, pdl: float, pdc: float, symbol: str = None):
    """
    Engine V2's level formula: PDH +/- Range/divisor (mirrored for support),
    with NO separate inner "reversal" zone -- V1's inner-vs-outer distinction
    doesn't have clean empirical support yet, so V2 keeps it simple: one
    resistance, one support, both closer to PDH/PDL than V1's.
    """
    divisor = V2_DIVISOR_BY_SYMBOL.get(symbol, V2_DEFAULT_DIVISOR)
    range_ = pdh - pdl
    resistance = pdh + range_ / divisor
    support = pdl - range_ / divisor
    return {
        "pdh": round(pdh, 2), "pdl": round(pdl, 2), "pdc": round(pdc, 2),
        "range": round(range_, 2), "divisor_used": divisor,
        "resistance": round(resistance, 2), "support": round(support, 2),
    }


def build_v2_probability_table(rows, atm, pcr, market_structure, history, underlying, symbol=None):
    """
    V2's S/R probability table -- same evidence-scoring as V1
    (evaluate_resistance/evaluate_support), fed V2's own level prices.
    Read-only: returns data for display, does not drive any trading action.
    """
    pdl_data = (market_structure or {}).get("prev_day")
    if not pdl_data:
        return None
    v2_levels = compute_v2_levels(pdl_data["pdh"], pdl_data["pdl"], pdl_data["pdc"], symbol=symbol)

    resistance_eval = evaluate_resistance(v2_levels["resistance"], rows, atm, pcr, market_structure, history, underlying)
    support_eval = evaluate_support(v2_levels["support"], rows, atm, pcr, market_structure, history, underlying)

    # OI-buildup confirmation: uses the ALREADY-computed ce_signal/pe_signal
    # at the ATM strike (classify_buildup runs live every cycle -- see
    # oi_engine.py) as an additional multi-factor confirmation gate.
    oi_buildup = None
    atm_row = next((r for r in rows if r.strike == atm), None)
    if atm_row is not None:
        from oi_engine import net_oi_buildup_lean
        oi_buildup = net_oi_buildup_lean(atm_row.ce_signal, atm_row.pe_signal)

    return {
        "levels": v2_levels,
        "resistance": resistance_eval,
        "support": support_eval,
        "oi_buildup": oi_buildup,
        "trend_and_signal": compute_v2_trend_and_signal(v2_levels, resistance_eval, support_eval, underlying, oi_buildup=oi_buildup),
    }


def compute_v2_trend_and_signal(levels, resistance_eval, support_eval, underlying, oi_buildup=None):
    """
    Simple, transparent (rule-based, NOT ML) trend-classification and signal
    for Engine V2, derived from the SAME evidence already scored by
    evaluate_resistance/evaluate_support -- no new/hidden scoring logic.

    Trend:
      - BULLISH: price above PDC (yesterday's close) AND support is holding
        (reversal_probability > breakdown_probability at support)
      - BEARISH: price below PDC AND resistance is holding (reversal >
        breakout at resistance)
      - RANGE: neither condition clearly met

    Signal (only when trend is directional AND the corresponding level's
    probability clears 60%, matching V1's SIGNAL_CONFIDENCE_THRESHOLD
    convention):
      - BUY CE: BULLISH trend + resistance.breakout_probability >= 60
                (a breakout continuation play), OR BULLISH trend +
                support.reversal_probability >= 60 (a support-bounce play)
      - BUY PE: mirrored for BEARISH trend
      - NO_TRADE: otherwise

    oi_buildup (optional, backward-compatible -- omit for identical behavior
    to before): the ATM strike's net_oi_buildup_lean() result (see
    oi_engine.py). When provided, this is used as an ADDITIONAL confirmation
    gate -- a signal only fires if the OI-buildup-lean genuinely AGREES with
    the signal's direction (standard "multi-factor confirmation" principle:
    fewer signals, but each with independent price-action AND OI-flow
    agreement, rather than price-action alone).
    """
    if underlying is None or levels.get("pdc") is None:
        return {"trend": "UNKNOWN", "signal": "NO_TRADE", "reason": "Missing underlying or PDC data"}

    above_pdc = underlying > levels["pdc"]
    support_holding = support_eval["reversal_probability"] > support_eval["breakdown_probability"]
    resistance_holding = resistance_eval["reversal_probability"] > resistance_eval["breakout_probability"]

    if above_pdc and support_holding:
        trend = "BULLISH"
    elif not above_pdc and resistance_holding:
        trend = "BEARISH"
    else:
        trend = "RANGE"

    signal, reason = "NO_TRADE", "No directional edge clears the 60% threshold"
    if trend == "BULLISH":
        if resistance_eval["breakout_probability"] >= 60:
            signal, reason = "BUY CE", f"Bullish trend + {resistance_eval['breakout_probability']}% breakout probability at resistance"
        elif support_eval["reversal_probability"] >= 60:
            signal, reason = "BUY CE", f"Bullish trend + {support_eval['reversal_probability']}% reversal probability at support"
    elif trend == "BEARISH":
        if support_eval["breakdown_probability"] >= 60:
            signal, reason = "BUY PE", f"Bearish trend + {support_eval['breakdown_probability']}% breakdown probability at support"
        elif resistance_eval["reversal_probability"] >= 60:
            signal, reason = "BUY PE", f"Bearish trend + {resistance_eval['reversal_probability']}% reversal probability at resistance"

    if oi_buildup is not None and signal != "NO_TRADE":
        wants_bullish_confirmation = signal == "BUY CE"
        oi_agrees = (oi_buildup["overall"] == "BULLISH") if wants_bullish_confirmation else (oi_buildup["overall"] == "BEARISH")
        if not oi_agrees:
            reason = f"{reason} -- BLOCKED: OI-buildup ({oi_buildup['overall']}, CE={oi_buildup.get('ce_lean')}, PE={oi_buildup.get('pe_lean')}) does not confirm"
            signal = "NO_TRADE"

    return {"trend": trend, "signal": signal, "reason": reason}
