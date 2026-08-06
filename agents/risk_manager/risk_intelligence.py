"""
agents/risk_manager/risk_intelligence.py -- "Learn from previous losses.
Detect repeated failure patterns. Recommend safer parameters. Compare
proposed strategy with historical failures stored in Memory. Never
recommend a configuration that previously failed without explaining
why." The ONLY module in agents/risk_manager/ that touches
agents.memory -- risk_engine.py's math stays a pure function of its
arguments (position sizing, VaR, drawdown simulation, ...); this module
supplies those arguments FROM memory and adds the "have we seen this
before" layer risk_engine.py has no way to know about on its own.
"""
import dataclasses

from .. import config
from . import risk_engine


@dataclasses.dataclass
class FailurePatternMatch:
    strategy_family: str
    occurrences: int
    reasons: list
    is_repeated: bool  # more than one occurrence -- a pattern, not a one-off


@dataclasses.dataclass
class ParameterRecommendation:
    parameter: str
    current_value: float
    suggested_value: float
    reason: str


def build_active_strategies(memory_store, *, exclude_strategy_name=None, limit=1000) -> list:
    """Everything currently promoted (is_best=True parameter sets) --
    the "portfolio" risk_engine's exposure/correlation checks compare a
    new candidate against.

    Two honest limitations, not placeholders:
    - risk_pct isn't captured by agents.memory.record_parameter_set
      today (Milestone 4/5 never needed it), so every active strategy is
      conservatively assumed to risk config.RISK_MAX_RISK_PER_TRADE_PCT --
      the same ceiling a new candidate is held to -- rather than
      inventing a number that was never recorded.
    - Raw trade histories aren't stored per parameter set either (only
      aggregate `performance` stats); real per-trade correlation needs
      agent_memory_backtest_history's optional `trades` column (added
      this milestone -- see sqlite_store.py's record_backtest/
      list_backtest_history). This does a best-effort same-symbol lookup
      for that; see _trades_for_symbol's docstring for exactly what
      "best-effort" means here."""
    rows = memory_store.search_parameter_sets(limit=limit)
    active = []
    for r in rows:
        if not r.get("is_best"):
            continue
        if exclude_strategy_name and r.get("strategy_name") == exclude_strategy_name:
            continue
        active.append({
            "strategy_name": r.get("strategy_name"), "symbol": r.get("symbol"),
            "risk_pct": config.RISK_MAX_RISK_PER_TRADE_PCT,
            "trades": _trades_for_symbol(memory_store, r.get("symbol")),
        })
    return active


def _trades_for_symbol(memory_store, symbol) -> list:
    """Best-effort: the most recently recorded backtest for this symbol's
    raw trades, if any were persisted. This is a same-SYMBOL lookup, not
    a same-STRATEGY lookup -- agent_memory_backtest_history has no
    strategy_name column (it predates per-strategy promotions; see
    Milestone 4's design). When more than one strategy is active on the
    same symbol, this may return a different (but same-symbol) strategy's
    trades. Correlation computed from this is still meaningful (it's
    real market-day P&L for that symbol) but should be read as
    "correlation with recent activity on this symbol," not a guaranteed
    per-strategy match. Returns [] (not fabricated data) when nothing
    was persisted with trades."""
    if not symbol:
        return []
    hits = memory_store.list_backtest_history(symbol=symbol, limit=1)
    return hits[0].get("trades") or [] if hits else []


def detect_failure_patterns(memory_store, *, strategy_family: str, limit: int = 50) -> FailurePatternMatch:
    """Searches Failed Experiment Memory for this strategy family and
    groups every hit into one pattern -- more than one occurrence is a
    RECURRING failure mode, not a one-off."""
    hits = memory_store.search_failed_experiments(strategy_family, limit=limit)
    relevant = [h for h in hits if strategy_family in (h.get("description") or "")]
    reasons = [h.get("reason") for h in relevant if h.get("reason")]
    return FailurePatternMatch(
        strategy_family=strategy_family, occurrences=len(relevant),
        reasons=reasons, is_repeated=len(relevant) > 1,
    )


def _thresholds_similar(a: dict, b: dict, *, tolerance_pct: float) -> bool:
    """True when every key A and B have in common is numerically within
    tolerance_pct of each other, and there's at least one shared key --
    an empty intersection is "we can't compare these," not "they match.\""""
    shared = set(a) & set(b)
    if not shared:
        return False
    for key in shared:
        va, vb = a.get(key), b.get(key)
        if not isinstance(va, (int, float)) or not isinstance(vb, (int, float)):
            continue
        base = max(abs(va), abs(vb), 1e-9)
        if abs(va - vb) / base * 100 > tolerance_pct:
            return False
    return True


def compare_with_historical_failures(memory_store, *, strategy_family: str,
                                      candidate_thresholds: dict, limit: int = 50):
    """Returns (is_known_bad: bool, matching_failures: list, explanation: str).
    "Known bad" means a PAST failed_experiment's recorded thresholds are
    numerically close to the candidate's own -- not just "same strategy
    family," which would flag nearly everything."""
    hits = memory_store.search_failed_experiments(strategy_family, limit=limit)
    matches = []
    for h in hits:
        past_thresholds = (h.get("parameters") or {}).get("thresholds") or {}
        if past_thresholds and _thresholds_similar(
            past_thresholds, candidate_thresholds, tolerance_pct=config.RISK_PARAMETER_SIMILARITY_PCT
        ):
            matches.append(h)
    if not matches:
        return False, [], "No past failure used a numerically similar configuration."
    explanation = (
        f"{len(matches)} past failure(s) used thresholds within "
        f"{config.RISK_PARAMETER_SIMILARITY_PCT:.0f}% of this candidate's own "
        f"(most recent reason: {matches[0].get('reason')!r})."
    )
    return True, matches, explanation


def recommend_safer_parameters(candidate_thresholds: dict, matching_failures: list) -> list:
    """For each threshold key a matching past failure also used, suggests
    moving 20% further away from the average failed value -- a generic,
    always-applicable "put more safety margin between this and the known
    failure" heuristic (threshold semantics vary per feature, so a
    feature-specific "correct direction" isn't knowable here; the
    magnitude/direction of the nudge is always AWAY from the failed
    value, which is the one thing that's always safe to assert)."""
    recommendations = []
    for key, candidate_value in candidate_thresholds.items():
        if not isinstance(candidate_value, (int, float)):
            continue
        failed_values = [
            (f.get("parameters") or {}).get("thresholds", {}).get(key)
            for f in matching_failures
        ]
        failed_values = [v for v in failed_values if isinstance(v, (int, float))]
        if not failed_values:
            continue
        avg_failed = sum(failed_values) / len(failed_values)
        gap = abs(candidate_value - avg_failed)
        nudge = gap * 0.2 if gap > 0 else (abs(candidate_value) * 0.2 or 0.1)
        direction = 1 if candidate_value >= avg_failed else -1
        suggested = round(candidate_value + direction * nudge, 6)
        recommendations.append(ParameterRecommendation(
            parameter=key, current_value=candidate_value, suggested_value=suggested,
            reason=f"past failures used {key}≈{avg_failed:.4g}; suggesting more distance for safety margin",
        ))
    return recommendations


def assess(memory_store, *, candidate_name: str, symbol: str, strategy_family: str,
           stop_points: float, trades: list, candidate_thresholds: dict,
           capital: float | None = None) -> risk_engine.RiskAssessment:
    """The Risk Manager's single entry point for a promotion candidate:
    builds real portfolio context from memory, runs the pure risk math,
    then folds in failure-pattern detection and "never silently repeat a
    known failure" -- if the candidate's own numbers alone would say
    APPROVED but its configuration is numerically close to a recorded
    failure, this downgrades the decision to REQUIRES_REVIEW and
    explains exactly why, rather than letting a clean-looking backtest
    silently repeat a mistake that's already on record."""
    active_strategies = build_active_strategies(memory_store, exclude_strategy_name=strategy_family)
    assessment = risk_engine.evaluate_promotion(
        candidate_name=candidate_name, symbol=symbol, strategy_family=strategy_family,
        stop_points=stop_points, trades=trades, active_strategies=active_strategies, capital=capital,
    )

    pattern = detect_failure_patterns(memory_store, strategy_family=strategy_family)
    is_known_bad, matches, comparison_explanation = compare_with_historical_failures(
        memory_store, strategy_family=strategy_family, candidate_thresholds=candidate_thresholds,
    )
    recommendations = recommend_safer_parameters(candidate_thresholds, matches) if matches else []

    extra_notes = []
    if pattern.is_repeated:
        extra_notes.append(
            f"{pattern.occurrences} prior failures recorded for {strategy_family} -- repeated failure pattern."
        )
    decision = assessment.decision
    if is_known_bad:
        extra_notes.append(comparison_explanation)
        if decision == "APPROVED":
            decision = "REQUIRES_REVIEW"
            extra_notes.append(
                "Downgraded from APPROVED to REQUIRES_REVIEW: this configuration is numerically close to "
                "a recorded failure -- a human should confirm this attempt is genuinely different before "
                "it proceeds, never silently repeated."
            )
    if recommendations:
        extra_notes.append(
            "Safer parameter suggestions: " +
            "; ".join(f"{r.parameter} {r.current_value}->{r.suggested_value}" for r in recommendations)
        )

    explanation = assessment.explanation
    if extra_notes:
        explanation = explanation + " " + " ".join(extra_notes)

    return dataclasses.replace(
        assessment, decision=decision, explanation=explanation,
        failure_pattern=pattern, known_bad_configuration=is_known_bad,
        parameter_recommendations=recommendations,
    )
