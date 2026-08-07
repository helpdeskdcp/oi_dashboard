"""
agents/trading_intelligence/explainability.py -- Milestone 11, Module
11.4: Explainable AI Reasoning.

Deviation from MILESTONE11_PLAN.md's original M11.4 sketch (an LLM
turning the existing structured reasoning strings into one narrative
paragraph via agents.llm_providers.generate_with_fallback()): this
milestone's own explicit implementation directive for this module
requires explanations to be deterministic and reproducible for the same
inputs, generated ONLY from real, already-computed BATI evidence, and
forbids fabricated reasoning, hallucinated confidence, placeholder AI, or
any external LLM call. An LLM-narrated paragraph -- even a faithful one
-- cannot satisfy "deterministic and reproducible for the same inputs"
by construction (a provider can rephrase, drop, or embellish a detail
differently across calls). This module is therefore a template-based
synthesis of exactly the structured signals every M10/M11 module already
computes -- never a narrative model, never a second source of truth.

Two entry points, matching the two places this engine has real,
measurable reasoning to explain:

- explain_recommendation(): why a live BUY/HOLD/NO_TRADE call looks the
  way it does -- confidence, probability calibration, regime (Module
  11.1), timeframe confirmation (Module 11.2), institutional persistence,
  and the existing structured reasoning strings ai_trading_engine.py
  already produces. Never re-derives any of these numbers; only narrates
  them.

- explain_trade_quality(): why a CLOSED trade received the Trade Quality
  Score it did (Module 11.3) -- setup strength, outcome alignment, and
  which of the three entry-time components (regime/timeframe/
  institutional) were actually captured for that trade.

Both are pure functions of their inputs -- no randomness, no clock, no
network call -- so the same inputs always produce the exact same output
string, satisfying the determinism requirement directly rather than by
convention.
"""
import dataclasses

from . import regime_profile, timeframe_confirmation, trade_quality


@dataclasses.dataclass
class Explanation:
    symbol: str
    summary: str  # the full narrative, evidence sentences joined with a space
    evidence: list  # list[str], the individual sentences summary is built from, in order
    inputs_used: list  # list[str], which named measurable inputs actually contributed (never includes an input that was unavailable)


def _trend_sentence(regime) -> str | None:
    """None when the trend regime itself is UNKNOWN (nothing to say) --
    never a fabricated "regime unclear" filler sentence."""
    if regime is None or regime.trend_regime == "UNKNOWN":
        return None
    vol = f", {regime.volatility_regime.lower()} volatility" if regime.volatility_regime != "UNKNOWN" else ""
    return f"Market regime is {regime.trend_regime.lower()}{vol}."


def _persistence_sentence(regime, *, direction: str | None) -> str | None:
    """Independently sourced from strikes history, not from ADX -- so
    this can be available even when the trend regime itself is UNKNOWN,
    and must not be dropped just because _trend_sentence() had nothing
    to say."""
    if regime is None or direction not in ("CE", "PE"):
        return None
    persistent = regime.ce_buildup_persistent if direction == "CE" else regime.pe_buildup_persistent
    if not persistent:
        return None
    cycles = regime.ce_persistence_cycles if direction == "CE" else regime.pe_persistence_cycles
    return (
        f"{direction} buildup has persisted for {cycles} consecutive cycles -- "
        f"sustained positioning, not a one-cycle blip."
    )


def _alignment_sentence(alignment) -> str | None:
    if alignment is None or alignment.alignment_label == "UNKNOWN":
        return None
    return (
        f"Timeframe confirmation is {alignment.alignment_label.lower()} "
        f"({alignment.agreement_count}/{alignment.available_count} higher timeframes agree, "
        f"{alignment.alignment_score}% alignment)."
    )


def explain_recommendation(recommendation, *, regime=None, alignment=None) -> Explanation:
    """The full Module 11.4 narrative for one ai_trading_engine.
    Recommendation. Never raises.

    `regime`/`alignment`: pass these when the caller already computed
    them this cycle (the same dedup discipline evaluate()'s own
    `snapshot`/`findings` params already establish). Left None, this
    function fetches them itself -- but only when `recommendation.
    direction` is set (NO_TRADE has no direction to align a regime/
    timeframe read against, so nothing is fetched or fabricated)."""
    evidence = []
    inputs_used = []

    if recommendation.action in ("BUY CE", "BUY PE"):
        evidence.append(
            f"{recommendation.action} on {recommendation.symbol} at {recommendation.entry_price} "
            f"(confidence {recommendation.confidence}/100, risk score {recommendation.risk_score}/100)."
        )
        inputs_used.append("confidence")
    elif recommendation.action == "HOLD":
        evidence.append(f"HOLD on {recommendation.symbol} -- an open position already exists.")
    else:
        evidence.append(f"NO_TRADE on {recommendation.symbol}.")

    if recommendation.probability is not None:
        evidence.append(f"Historical calibration for this confidence bucket: {recommendation.probability}% win rate.")
        inputs_used.append("probability")
    elif recommendation.probability_note:
        evidence.append(f"Probability not yet calibrated -- {recommendation.probability_note}.")

    if recommendation.direction in ("CE", "PE"):
        if regime is None:
            regime = regime_profile.classify(recommendation.symbol)
        if alignment is None:
            alignment = timeframe_confirmation.check(recommendation.symbol, direction=recommendation.direction)

        trend_sentence = _trend_sentence(regime)
        if trend_sentence:
            evidence.append(trend_sentence)
            inputs_used.append("regime")

        persistence_sentence = _persistence_sentence(regime, direction=recommendation.direction)
        if persistence_sentence:
            evidence.append(persistence_sentence)
            inputs_used.append("institutional_persistence")

        alignment_sentence = _alignment_sentence(alignment)
        if alignment_sentence:
            evidence.append(alignment_sentence)
            inputs_used.append("timeframe_alignment")

    if recommendation.institutional_reasoning:
        evidence.append(f"Institutional flow: {recommendation.institutional_reasoning}")
        inputs_used.append("institutional_reasoning")

    return Explanation(
        symbol=recommendation.symbol, summary=" ".join(evidence), evidence=evidence, inputs_used=inputs_used,
    )


def explain_trade_quality(trade: dict) -> Explanation:
    """The full Module 11.4 narrative for why one CLOSED trade received
    the trade_quality.score() it did. Never raises -- a trade with no
    captured entry-time context explains THAT honestly rather than
    guessing why it scored the way it did."""
    evidence = []
    inputs_used = []

    points = trade.get("points")
    if points is None:
        evidence.append(f"Trade on {trade.get('symbol')} is not closed yet -- no outcome to explain.")
        return Explanation(symbol=trade.get("symbol"), summary=" ".join(evidence), evidence=evidence, inputs_used=[])

    outcome = "won" if points > 0 else "lost"
    evidence.append(f"Trade on {trade.get('symbol')} {outcome} ({points:+.2f} pts, {trade.get('exit_reason')}).")
    inputs_used.append("outcome")

    quality = trade_quality.score(trade)
    if not quality.available:
        evidence.append(f"Trade Quality Score unavailable -- {quality.reason}.")
        return Explanation(symbol=trade.get("symbol"), summary=" ".join(evidence), evidence=evidence, inputs_used=inputs_used)

    tier = trade_quality.quality_tier(quality.score)
    evidence.append(
        f"Trade Quality Score: {quality.score}/100 ({tier}) -- setup strength {quality.setup_strength}/100, "
        f"outcome alignment {quality.outcome_alignment}/100."
    )
    inputs_used.append("trade_quality")

    if quality.components.get("regime") is not None:
        evidence.append(
            f"Regime component: {quality.components['regime']}/100 "
            f"(trend was {trade.get('regime_trend_at_entry')} at entry)."
        )
        inputs_used.append("regime")

    if quality.components.get("timeframe") is not None:
        evidence.append(f"Timeframe alignment component: {quality.components['timeframe']}/100 at entry.")
        inputs_used.append("timeframe_alignment")

    if quality.components.get("institutional") is not None:
        backed = "backed" if quality.components["institutional"] == 100.0 else "did not back"
        evidence.append(f"An institutional finding {backed} this trade's direction at entry.")
        inputs_used.append("institutional_persistence")

    return Explanation(
        symbol=trade.get("symbol"), summary=" ".join(evidence), evidence=evidence, inputs_used=inputs_used,
    )
