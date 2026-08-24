"""
signal_qualification.py -- Signal Intelligence V2, Stage B: production
signal qualification.

WHY THIS EXISTS: oi_engine.generate_signal() (Stage A, unchanged by this
module) already recognizes a weak setup in its own reasoning text (e.g.
"not near any major level ... low conviction ... skip unless everything
else is very strong") but still returns action="BUY CE" -- nothing
downstream acts on that recognition. `tradeable`/`confidence` are
informational fields a caller COULD check, but the real gap is that
`action` alone doesn't distinguish "here is a directional read" from
"here is something worth acting on."

This module is Stage B: given an already-actionable Stage-A Recommendation
(action already "BUY CE"/"BUY PE", already past ai_trading_engine.evaluate()'s
EXPIRY_NOT_RESOLVED/INVALID_OPTION_CONTRACT gates), it computes a
`production_action` -- one of PRODUCTION_ACTIONS below -- by REUSING existing,
already-tested evidence sources wholesale, never re-deriving them:

  - failure_gate.run_failure_checks() -- reward:risk, confidence/tradeable,
    regime, major-level-proximity. All four checks already exist; this
    module does not reimplement any of them.
  - regime_profile._breakout_confirmation() -- VWAP alignment + OI-lean +
    premium-momentum + volume-expansion, via sr_probability_engine's own
    fake_breakout_filter(). Already computes exactly the four confirmations
    section 6/8 of the brief ask for; reused directly.
  - expiry_context (agents.trading_intelligence.expiry_intelligence.
    compute_scalping_metrics()'s output, already threaded through
    ai_trading_engine.evaluate()) -- gamma_risk_zone/theta_decay_mode/
    expiry_pressure_score, for EXPIRY_DAY_MODE's stricter thresholds.

SHADOW ONLY (config.TI_ENABLE_SIGNAL_QUALITY_V2, default OFF): this module
never changes action/direction/entry/target/SL/confidence/qty. It is called
from ai_trading_engine.evaluate() the same way the regime-filter and
failure-gate shadow blocks are -- wrapped in try/except, attached to the
Recommendation purely for observation/logging. Turning production_action
into what actually gates Telegram sends or paper-trade entry is a decision
for AFTER this has real backtest evidence behind it
(see signal_quality_v2_backtest.py), matching this project's own
established discipline for every prior signal-affecting change
(TI_ENABLE_REGIME_FILTER_SHADOW, TI_ENABLE_FAILURE_GATE_SHADOW before it).

PRODUCTION_CONFIDENCE IS NOT A CALIBRATED PROBABILITY. It is a second,
independent evidence-weighted score (like `confidence` itself already is,
per the existing UI disclaimer in confidence_badge.js: "signal strength,
not a win probability") -- never conflate it with `probability`
(ai_trading_engine._calibrated_probability()'s empirical bucketed win
rate, passed through here unchanged). The specific point weights below are
a first-pass, evidence-informed but NOT backtested starting point --
signal_quality_v2_backtest.py exists specifically to check whether they
generalize before any real activation. Do not hand-tune them to make one
example look good; see that script's own docstring.
"""
import dataclasses

from oi_engine import net_oi_buildup_lean
from . import failure_gate, regime_profile

# Production-action vocabulary. Deliberately plain string constants
# (matching failure_gate.py's PASS/FAIL/NOT_EVALUATED convention), not an
# enum type -- every other status field in this package (action, regime,
# tradeability) already uses this same plain-string style.
ACTIONABLE_BUY_CE = "ACTIONABLE_BUY_CE"
ACTIONABLE_BUY_PE = "ACTIONABLE_BUY_PE"
WATCHLIST_CE = "WATCHLIST_CE"
WATCHLIST_PE = "WATCHLIST_PE"
BLOCKED_LOW_CONFIDENCE = "BLOCKED_LOW_CONFIDENCE"
BLOCKED_BAD_REGIME = "BLOCKED_BAD_REGIME"
BLOCKED_EXPIRY_RISK = "BLOCKED_EXPIRY_RISK"
BLOCKED_RISK_REWARD = "BLOCKED_RISK_REWARD"
BLOCKED_NO_LEVEL_CONFIRMATION = "BLOCKED_NO_LEVEL_CONFIRMATION"
NO_TRADE = "NO_TRADE"

PRODUCTION_ACTIONS = (
    ACTIONABLE_BUY_CE, ACTIONABLE_BUY_PE, WATCHLIST_CE, WATCHLIST_PE,
    BLOCKED_LOW_CONFIDENCE, BLOCKED_BAD_REGIME, BLOCKED_EXPIRY_RISK,
    BLOCKED_RISK_REWARD, BLOCKED_NO_LEVEL_CONFIRMATION, NO_TRADE,
)

# Reuses generate_signal()'s own confidence_threshold default (oi_engine.py)
# rather than inventing a second number for the same concept.
PRODUCTION_CONFIDENCE_ACTIONABLE_MIN = 60
# Below this, WATCHLIST/BLOCKED rather than ACTIONABLE even if every hard
# gate technically passes -- provisional, see module docstring.
PRODUCTION_CONFIDENCE_WATCHLIST_MIN = 40
# Expiry-day mode requires clearing a higher bar than a normal day --
# provisional, see module docstring; validate via signal_quality_v2_backtest.py
# before changing.
EXPIRY_DAY_CONFIDENCE_ACTIONABLE_MIN = 70

# Evidence-weight adjustments applied on top of Stage-A `confidence` to
# produce `production_confidence`. Provisional -- see module docstring.
WEIGHT_VWAP_CONTRADICTION_UNCONFIRMED = -25
WEIGHT_OI_DISAGREES_WITH_DIRECTION = -20
WEIGHT_BREAKOUT_FULLY_CONFIRMED = 10
WEIGHT_EXPIRY_DAY_UNCONFIRMED_PENALTY = -15


@dataclasses.dataclass
class EvidenceComponent:
    name: str
    direction: str  # "BULLISH" | "BEARISH" | "NEUTRAL" | "N/A"
    score: int
    reason: str
    contribution: int  # signed points this component added to production_confidence


@dataclasses.dataclass
class QualificationResult:
    production_action: str
    production_confidence: int | None
    probability: float | None  # pass-through of Stage A's _calibrated_probability(), never recomputed
    components: list
    reasons: list  # every failed/contradicted evidence item, not just the first
    signal_state_transitions: list
    explanation: dict


def _vwap_evidence(direction, breakout_failed):
    contradicted = "Price is trading against VWAP" in breakout_failed
    if contradicted:
        return EvidenceComponent(
            "vwap", direction, 0,
            f"spot is on the wrong side of VWAP for a {direction} trade", 0,
        ), True
    return EvidenceComponent("vwap", direction, 1, "spot is VWAP-aligned or VWAP unavailable", 0), False


def _oi_evidence(direction, rows, strike):
    atm_row = next((r for r in rows if r.strike == strike), None)
    ce_signal = atm_row.ce_signal if atm_row else "Neutral"
    pe_signal = atm_row.pe_signal if atm_row else "Neutral"
    lean = net_oi_buildup_lean(ce_signal, pe_signal)
    supports = (lean["overall"] == "BULLISH") if direction == "CE" else (lean["overall"] == "BEARISH")
    return EvidenceComponent(
        "oi", lean["overall"], lean["net_lean"],
        f"CE={ce_signal}, PE={pe_signal}, net_lean={lean['net_lean']}", 0,
    ), supports


def qualify_signal(symbol, *, direction, strike, entry_price, sl_price, target_price,
                    confidence, probability, tradeable, rows, atm, underlying,
                    support, resistance, market_structure, snapshot, expiry_date,
                    expiry_context, is_mcx) -> QualificationResult:
    """Shadow-only production qualification for an already-actionable Stage-A
    signal. Never raises -- ai_trading_engine.evaluate()'s own caller wraps
    this in try/except anyway (same convention as the regime/failure-gate
    shadow blocks), but every step here degrades honestly rather than
    crashing on missing data."""
    reasons = []
    components = []

    failure_report = failure_gate.run_failure_checks(
        symbol=symbol, direction=direction, entry_price=entry_price, sl_price=sl_price,
        target_price=target_price, confidence=confidence, tradeable=tradeable, rows=rows, atm=atm,
        underlying=underlying, support=support, resistance=resistance, market_structure=market_structure,
        snapshot=snapshot, expiry_date=expiry_date, is_mcx=is_mcx,
    )
    for check in failure_report.checks:
        components.append(EvidenceComponent(
            f"failure_gate.{check.name}", direction if check.status == "PASS" else "N/A",
            1 if check.status == "PASS" else (-1 if check.status == "FAIL" else 0),
            check.detail, 0,
        ))
    reasons.extend(f"{name} check failed" for name in failure_report.failed)

    breakout_passes, breakout_failed = regime_profile._breakout_confirmation(
        symbol, direction=direction, strike=strike, rows=rows, underlying=underlying,
        market_structure=market_structure,
    )

    production_confidence = confidence if confidence is not None else 0

    vwap_component, vwap_contradicted = _vwap_evidence(direction, breakout_failed)
    components.append(vwap_component)
    vwap_confirmed_by_other_evidence = vwap_contradicted and len(breakout_failed) == 1  # only VWAP failed
    if vwap_contradicted and not vwap_confirmed_by_other_evidence:
        production_confidence += WEIGHT_VWAP_CONTRADICTION_UNCONFIRMED
        vwap_component.contribution = WEIGHT_VWAP_CONTRADICTION_UNCONFIRMED
        reasons.append("spot is against VWAP with no breakout/volume/OI/momentum confirmation")
    elif vwap_contradicted:
        reasons.append("spot is against VWAP but confirmed by breakout/volume/OI/momentum evidence")

    oi_component, oi_supports = _oi_evidence(direction, rows, strike)
    components.append(oi_component)
    if not oi_supports:
        production_confidence += WEIGHT_OI_DISAGREES_WITH_DIRECTION
        oi_component.contribution = WEIGHT_OI_DISAGREES_WITH_DIRECTION
        reasons.append(f"OI lean ({oi_component.direction}) disagrees with {direction} direction")

    if breakout_passes:
        production_confidence += WEIGHT_BREAKOUT_FULLY_CONFIRMED
        components.append(EvidenceComponent(
            "breakout_confirmation", direction, 1,
            "volume expansion + OI + premium momentum + VWAP all confirm", WEIGHT_BREAKOUT_FULLY_CONFIRMED,
        ))
    else:
        components.append(EvidenceComponent(
            "breakout_confirmation", "N/A", 0, f"not fully confirmed: {breakout_failed}", 0,
        ))

    theta_decay_mode = bool((expiry_context or {}).get("theta_decay_mode"))
    gamma_risk_zone = bool((expiry_context or {}).get("gamma_risk_zone"))
    if theta_decay_mode:
        components.append(EvidenceComponent(
            "expiry_day_mode", "N/A", 0,
            f"expiry is today (theta_decay_mode) -- gamma_risk_zone={gamma_risk_zone}, "
            f"stricter thresholds apply", 0,
        ))
        if gamma_risk_zone and not breakout_passes:
            production_confidence += WEIGHT_EXPIRY_DAY_UNCONFIRMED_PENALTY
            reasons.append("expiry-day gamma risk zone with no breakout confirmation")

    production_confidence = max(0, min(100, production_confidence))
    # `probability` is passed through unchanged and never used as a hard gate --
    # PROBABILITY_CALIBRATION_AUDIT.md found it's not actually calibrated
    # (~23-25% actual regardless of bucket), so gating on it would be false
    # precision. It's still surfaced in the explanation for a human to weigh.

    ce_or_pe = direction
    actionable_action = ACTIONABLE_BUY_CE if ce_or_pe == "CE" else ACTIONABLE_BUY_PE
    watchlist_action = WATCHLIST_CE if ce_or_pe == "CE" else WATCHLIST_PE

    confidence_floor = EXPIRY_DAY_CONFIDENCE_ACTIONABLE_MIN if theta_decay_mode else PRODUCTION_CONFIDENCE_ACTIONABLE_MIN

    # Hard gates, in order -- first blocking reason sets production_action,
    # but every failure already collected above stays in `reasons`.
    if not tradeable or production_confidence < PRODUCTION_CONFIDENCE_WATCHLIST_MIN:
        production_action = BLOCKED_LOW_CONFIDENCE
    elif failure_report.status == failure_gate.STATUS_BLOCKED and "regime" in failure_report.failed:
        production_action = BLOCKED_BAD_REGIME
    elif failure_report.status == failure_gate.STATUS_BLOCKED and "reward_risk" in failure_report.failed:
        production_action = BLOCKED_RISK_REWARD
    elif theta_decay_mode and gamma_risk_zone and not breakout_passes and production_confidence < confidence_floor:
        production_action = BLOCKED_EXPIRY_RISK
    elif failure_report.status == failure_gate.STATUS_BLOCKED and "major_level_proximity" in failure_report.failed \
            and not breakout_passes:
        production_action = BLOCKED_NO_LEVEL_CONFIRMATION
    elif production_confidence < confidence_floor or not breakout_passes:
        production_action = watchlist_action
    else:
        production_action = actionable_action

    transitions = ["RAW_SIGNAL", "QUALIFYING", production_action]

    explanation = {
        "direction": f"BUY {direction}",
        "raw_confidence": confidence,
        "production_confidence": production_confidence,
        "probability": probability,
        "expiry_day_mode": theta_decay_mode,
        "gamma_risk_zone": gamma_risk_zone,
        "breakout_confirmed": breakout_passes,
        "reasons": reasons,
        "final": production_action,
    }

    return QualificationResult(
        production_action=production_action, production_confidence=production_confidence,
        probability=probability, components=components, reasons=reasons,
        signal_state_transitions=transitions, explanation=explanation,
    )
