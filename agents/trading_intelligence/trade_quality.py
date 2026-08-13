"""
agents/trading_intelligence/trade_quality.py -- Milestone 11, Module 11.3:
Trade Quality Scoring & Multi-Dimensional Calibration.

Every prior calibration surface in this repository (ai_trading_engine.
calibration_report(), backtest.score_calibration_report()) buckets closed
trades by a SINGLE dimension (confidence, or institutional-tier/regime)
and reports win-rate -- useful, but blind to WHY a trade won or lost.
This module adds a genuinely new, honest signal: did this trade's own
reasoning inputs (regime alignment from Module 11.1, timeframe
confirmation from Module 11.2, institutional-finding backing) actually
match what happened to it? A trade that was well-supported AND won is
high quality. A trade that was well-supported but lost is a fair bet that
didn't pay off (still meaningful -- not the same as a badly-reasoned
loss). A trade that had no real support but won anyway is a lucky
outcome, not evidence of good reasoning.

Design constraint this module holds to strictly: regime_profile.classify()
and timeframe_confirmation.check() both read the LATEST snapshot in their
respective tables -- neither supports "as of a past timestamp." Computing
them AFTER a trade has closed would silently score the trade against
TODAY's market state, not the state that actually existed at entry --
exactly the kind of retroactive fabrication this project's own honesty
discipline forbids (see multi_timeframe.py's 1m/5m precedent). So this
module's context (regime_trend_at_entry, regime_volatility_at_entry,
timeframe_alignment_score_at_entry, institutional_backed_at_entry) is
captured ONCE, live, at the moment paper_trading.enter_from_recommendation()
actually opens the trade, and persisted on the ti_paper_trades row itself
(see ti_store.py's new columns) -- never recomputed after the fact.
Trades opened before this instrumentation existed simply have no stored
context; score() degrades them honestly to `available=False` rather than
guessing.
"""
import dataclasses

from . import institutional_intelligence, ti_store

# Transparent, documented mapping from regime_profile.RegimeProfile.trend_regime
# to a 0-100 "how well does this regime support a directional options buy"
# score -- a TRENDING market backs a directional CE/PE far better than a
# RANGING one (the same sideways-market limitation already documented for
# this project's Ichimoku engine). "UNKNOWN" is deliberately absent from
# this dict -- an unavailable trend regime is EXCLUDED from scoring, not
# assigned a fabricated mid-point.
#
# This is a PRIOR, not a permanent belief -- see calibrated_regime_score()
# below, which lets this engine's own closed-trade history override it once
# there's enough of that history to trust. Kept here (rather than deleted)
# as the honest fallback for a regime that doesn't have that history yet.
REGIME_TREND_SCORE = {"TRENDING": 100.0, "TRANSITIONING": 60.0, "RANGING": 40.0}

# Same "don't trust a stat below this sample size" gate every calibration
# surface in this project uses (ai_trading_engine.CALIBRATION_MIN_SAMPLE,
# adaptive_sizing.TRACK_RECORD_MIN_SAMPLE).
CALIBRATED_REGIME_MIN_SAMPLE = 5


def calibrated_regime_score(trend_regime: str | None) -> tuple:
    """Live win-rate-based alternative to the static REGIME_TREND_SCORE
    prior above, queried fresh from this engine's own closed
    ti_paper_trades history -- same "no separate training step, every
    call queries live" discipline as ai_trading_engine.
    calibration_report()'s confidence-bucket calibration.

    Returns (score: float|None, note: str). score is None, with an
    honest note, whenever the regime is missing/"UNKNOWN" or this engine
    doesn't yet have CALIBRATED_REGIME_MIN_SAMPLE closed trades logged
    under it -- callers fall back to the static REGIME_TREND_SCORE prior
    in that case rather than treating None as zero.

    Confirmed live 2026-08-13: the static prior above had TRENDING=100
    (assumed the strongest setup) and RANGING=40 (assumed the weakest)
    backwards relative to this engine's actual paper-trade outcomes --
    18 closed TRENDING trades at a 5.6% win rate vs. 12 closed RANGING
    trades at 41.7%. This function lets real outcomes override that
    initial guess once there's enough history to trust, instead of
    requiring a manual code change every time the regime that actually
    performs best shifts."""
    if not trend_regime or trend_regime == "UNKNOWN":
        return None, "no regime to calibrate against"
    trades = [
        t for t in ti_store.list_closed_trades(limit=10_000)
        if t.get("regime_trend_at_entry") == trend_regime
    ]
    if len(trades) < CALIBRATED_REGIME_MIN_SAMPLE:
        return None, (
            f"insufficient history -- only {len(trades)} closed trade(s) in the "
            f"{trend_regime!r} regime (need >= {CALIBRATED_REGIME_MIN_SAMPLE})"
        )
    wins = sum(1 for t in trades if (t.get("points") or 0) > 0)
    pct = round(wins / len(trades) * 100, 1)
    return pct, f"live win rate across {len(trades)} closed trade(s) in the {trend_regime!r} regime"

# setup_strength (see score()) is a 0-100 average of whichever of the three
# reasoning components were actually available; this is the mid-point used
# to decide whether the entry itself was "confident" or "not confident"
# before comparing that to the real outcome.
SETUP_STRENGTH_CONFIDENT_THRESHOLD = 50.0

# The two 0-100 sub-scores combined into the final Trade Quality Score --
# setup_strength (was the entry well-reasoned) and outcome_alignment (did
# that reasoning's own confidence direction match what actually happened)
# weighted equally, matching strike_intelligence._ai_strike_score()'s own
# "named weights, transparent arithmetic, no fitted model" convention.
SETUP_STRENGTH_WEIGHT = 0.5
OUTCOME_ALIGNMENT_WEIGHT = 0.5
assert abs(SETUP_STRENGTH_WEIGHT + OUTCOME_ALIGNMENT_WEIGHT - 1.0) < 1e-9

# Tiering used only for calibration_report()'s optional "quality_tier"
# bucketing dimension (ai_trading_engine.py) -- a plain three-way split of
# the 0-100 score, not a separate scoring method. Half-open [lo, hi)
# ranges (quality_tier() below handles the closed upper bound at 100) --
# Phase 7 validation fix: the original (0,39)/(40,69)/(70,100) integer
# bounds left gaps at fractional values (e.g. 39.5, 69.3) that a REAL
# score -- round(setup_strength*0.5 + outcome_alignment*0.5, 1) is
# routinely fractional -- could fall into, misclassifying a real
# low/medium-quality trade as "UNKNOWN" instead.
QUALITY_TIERS = (("LOW", 0, 40), ("MEDIUM", 40, 70), ("HIGH", 70, 100))


@dataclasses.dataclass
class TradeQualityScore:
    trade_id: int | None
    available: bool
    score: float | None  # 0-100; None when available is False
    setup_strength: float | None  # 0-100; average of whichever components were present
    outcome_alignment: float | None  # 0-100; 100 if setup confidence direction matched the real outcome
    components: dict  # {"regime": float|None, "timeframe": float|None, "institutional": float|None}
    reason: str | None  # populated only when available is False


def _regime_component(trend_regime: str | None) -> float | None:
    """None (excluded, not scored) for a missing or "UNKNOWN" regime --
    honest absence of data, never a fabricated mid-point."""
    if not trend_regime:
        return None
    return REGIME_TREND_SCORE.get(trend_regime)


def _institutional_component(backed: bool | None) -> float | None:
    if backed is None:
        return None
    return 100.0 if backed else 0.0


def institutional_backing(symbol: str, *, direction: str, strike: int | None,
                           snapshot=None, findings: list | None = None) -> bool | None:
    """True if institutional_intelligence found a real InstitutionalBuying/
    InstitutionalSelling finding at this exact strike whose own recorded
    side agrees with `direction`; False if findings were available but
    none agreed; None if institutional data itself was unavailable this
    cycle (honest degradation, matching every other read in this package).

    `snapshot`/`findings`: pass these when the caller (paper_trading.
    enter_from_recommendation(), the normal path) already computed them
    this cycle -- avoids a second institutional-intelligence sweep for
    the same data, the same dedup discipline evaluate() itself uses."""
    if findings is None:
        result = institutional_intelligence.analyze(symbol, snapshot=snapshot)
        if not result["available"]:
            return None
        findings = result["findings"]

    for f in findings:
        if f.pattern_type in ("InstitutionalBuying", "InstitutionalSelling") and f.strike == strike:
            if f.evidence.get("side") == direction:
                return True
    return False


def score(trade: dict) -> TradeQualityScore:
    """The full Module 11.3 read for one CLOSED ti_paper_trades row (as
    returned by ti_store.list_closed_trades()/list_open_trades()). Never
    raises. Requires a genuinely closed trade with a real outcome --
    quality is a statement about reasoning-vs-outcome, which doesn't
    exist yet for an open position."""
    if trade.get("status") != "CLOSED" or trade.get("points") is None:
        return TradeQualityScore(
            trade_id=trade.get("id"), available=False, score=None, setup_strength=None,
            outcome_alignment=None, components={},
            reason="trade is not CLOSED yet -- quality score only applies to a real, final outcome",
        )

    components = {
        "regime": _regime_component(trade.get("regime_trend_at_entry")),
        "timeframe": trade.get("timeframe_alignment_score_at_entry"),
        "institutional": _institutional_component(trade.get("institutional_backed_at_entry")),
    }
    available = [v for v in components.values() if v is not None]
    if not available:
        return TradeQualityScore(
            trade_id=trade.get("id"), available=False, score=None, setup_strength=None,
            outcome_alignment=None, components=components,
            reason="no entry-time reasoning context recorded for this trade -- opened before "
                   "Module 11.3 instrumentation, or every reading was unavailable that cycle",
        )

    setup_strength = round(sum(available) / len(available), 1)
    won = (trade.get("points") or 0) > 0
    confident = setup_strength >= SETUP_STRENGTH_CONFIDENT_THRESHOLD
    # Did the entry's own confidence direction match the real outcome --
    # a confident setup that won, or a non-confident setup that lost, is
    # "aligned" (100); a confident setup that lost or a non-confident
    # setup that won anyway is a "surprise" (0). Binary and transparent
    # on purpose -- no invented partial-credit bucket table.
    outcome_alignment = 100.0 if confident == won else 0.0

    total = round(setup_strength * SETUP_STRENGTH_WEIGHT + outcome_alignment * OUTCOME_ALIGNMENT_WEIGHT, 1)
    return TradeQualityScore(
        trade_id=trade.get("id"), available=True, score=total, setup_strength=setup_strength,
        outcome_alignment=outcome_alignment, components=components, reason=None,
    )


def quality_tier(quality_score: float | None) -> str:
    """LOW/MEDIUM/HIGH from a 0-100 score, "UNKNOWN" for None -- used only
    by ai_trading_engine.calibration_report()'s optional quality_tier
    bucketing dimension. Half-open [lo, hi) per QUALITY_TIERS tier, except
    the last (HIGH), whose upper bound (100) is closed -- covers every
    real value in [0, 100] with no gap, including fractional scores."""
    if quality_score is None:
        return "UNKNOWN"
    for label, lo, hi in QUALITY_TIERS:
        if lo <= quality_score < hi:
            return label
    if quality_score == QUALITY_TIERS[-1][2]:  # exactly 100.0, the one closed upper bound
        return QUALITY_TIERS[-1][0]
    return "UNKNOWN"
