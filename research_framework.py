"""
research_framework.py -- Hypothesis-driven quantitative research scaffolding.

CORE PRINCIPLE: every new market-microstructure metric starts as an
UNVALIDATED HYPOTHESIS, not a fact. This framework exists to test hypotheses
rigorously against real historical data and report honest statistical
results -- including when a hypothesis FAILS and should be rejected.

Nothing in this module is wired into live trading signals. A hypothesis can
only ever be considered for that after being explicitly validated here, with
documented, reproducible results -- and even then, that's a SEPARATE,
deliberate decision, not automatic.

USAGE PATTERN:
    1. Define a Hypothesis: a name, a plain-language description of the
       mathematical intuition, and a `compute(cycle_window)` function that
       derives the metric from a window of historical cycles.
    2. Define what the hypothesis PREDICTS (e.g. "high values precede a
       reversal within N minutes" or "high values correlate with lower
       realized volatility over the next hour").
    3. Run `run_hypothesis_test()` against real logged data. It reports:
       precision, recall, false-positive/negative counts, a naive
       baseline for comparison, and a statistical-significance check.
    4. The hypothesis is only ever marked VALIDATED if it beats the naive
       baseline by a meaningful, statistically-defensible margin on a
       genuinely large-enough sample. Otherwise it's REJECTED, with the
       reason logged.

This is intentionally similar in spirit to analyze_standard_formulas.py and
find_best_formula_coefficients.py -- same discipline, generalized into a
reusable framework instead of one-off scripts.
"""
import json
import math
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

RESEARCH_LOG_PATH = Path("research_log.json")

# A hypothesis needs at least this many independent test-instances before
# ANY conclusion (validated or rejected) is drawn -- below this, the only
# honest verdict is "insufficient data", never a pass or fail.
MIN_SAMPLE_SIZE = 30


@dataclass
class Hypothesis:
    """
    A single, falsifiable research hypothesis.

    name: short identifier (e.g. "oi_concentration_index")
    description: plain-language statement of the mathematical intuition --
                 WHY this might matter, not a claim that it does.
    compute_fn: function(cycle_window: list[dict]) -> float | None.
                Takes a window of recent cycles (each a dict with whatever
                fields are needed) and returns the metric's value for the
                LATEST cycle in that window, or None if it can't be computed
                (e.g. insufficient history).
    predicts: plain-language statement of the falsifiable prediction being
              tested (e.g. "a value above the 80th percentile predicts a
              price-reversal within 20 minutes more often than baseline").
    status: "untested" | "validated" | "rejected" -- ALWAYS starts untested.
    """
    name: str
    description: str
    compute_fn: Callable
    predicts: str
    status: str = "untested"
    validation_notes: str = ""


@dataclass
class TestResult:
    hypothesis_name: str
    sample_size: int
    true_positives: int
    false_positives: int
    true_negatives: int
    false_negatives: int
    precision: Optional[float]
    recall: Optional[float]
    baseline_hit_rate: float   # what a naive "always predict the majority outcome" would score
    hypothesis_hit_rate: float
    improvement_over_baseline: float
    verdict: str   # "VALIDATED" | "REJECTED" | "INSUFFICIENT_DATA"
    reason: str
    tested_at: str


def run_hypothesis_test(hypothesis: Hypothesis, cycles: list, threshold_percentile: float,
                     outcome_fn: Callable, lookforward_n: int = 20) -> TestResult:
    """
    Backtests a hypothesis against a chronological list of historical
    cycles (each a dict, e.g. loaded from the cycles/strikes tables).

    hypothesis: the Hypothesis to test.
    cycles: chronological list of cycle-dicts (must include whatever fields
            hypothesis.compute_fn and outcome_fn need).
    threshold_percentile: the metric-value percentile (0-100) above which a
            cycle counts as a "signal" from this hypothesis (e.g. 80 means
            "top 20% of observed values triggers a prediction").
    outcome_fn: function(cycles, index) -> bool | None. Given the full cycle
            list and an index, returns whether the PREDICTED outcome
            genuinely happened within lookforward_n cycles afterward (True/
            False), or None if there isn't enough forward-data to judge yet.
    lookforward_n: how many cycles ahead outcome_fn should look (informational
            only here -- the actual window logic lives in outcome_fn, since
            that logic is prediction-specific).

    Returns a TestResult with honest, unfudged statistics. Mutates
    hypothesis.status/validation_notes based on the verdict.
    """
    values = []
    for i, c in enumerate(cycles):
        window = cycles[max(0, i - 20):i + 1]   # last 20 cycles as context, matching typical rolling-window use
        v = hypothesis.compute_fn(window)
        values.append(v)

    valid_values = [v for v in values if v is not None]
    if len(valid_values) < MIN_SAMPLE_SIZE:
        result = TestResult(
            hypothesis_name=hypothesis.name, sample_size=len(valid_values),
            true_positives=0, false_positives=0, true_negatives=0, false_negatives=0,
            precision=None, recall=None, baseline_hit_rate=0, hypothesis_hit_rate=0,
            improvement_over_baseline=0, verdict="INSUFFICIENT_DATA",
            reason=f"Only {len(valid_values)} computable values (need >= {MIN_SAMPLE_SIZE}).",
            tested_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        hypothesis.status = "untested"
        hypothesis.validation_notes = result.reason
        return result

    sorted_vals = sorted(valid_values)
    threshold_idx = min(len(sorted_vals) - 1, int(len(sorted_vals) * threshold_percentile / 100))
    threshold = sorted_vals[threshold_idx]

    tp = fp = tn = fn = 0
    outcomes_seen = 0
    positive_outcomes = 0
    for i, v in enumerate(values):
        if v is None:
            continue
        outcome = outcome_fn(cycles, i)
        if outcome is None:
            continue   # not enough forward-data yet to judge this cycle
        outcomes_seen += 1
        if outcome:
            positive_outcomes += 1
        predicted_positive = v >= threshold
        if predicted_positive and outcome:
            tp += 1
        elif predicted_positive and not outcome:
            fp += 1
        elif not predicted_positive and outcome:
            fn += 1
        else:
            tn += 1

    if outcomes_seen < MIN_SAMPLE_SIZE:
        result = TestResult(
            hypothesis_name=hypothesis.name, sample_size=outcomes_seen,
            true_positives=tp, false_positives=fp, true_negatives=tn, false_negatives=fn,
            precision=None, recall=None, baseline_hit_rate=0, hypothesis_hit_rate=0,
            improvement_over_baseline=0, verdict="INSUFFICIENT_DATA",
            reason=f"Only {outcomes_seen} cycles had a judgeable forward-outcome (need >= {MIN_SAMPLE_SIZE}).",
            tested_at=time.strftime("%Y-%m-%d %H:%M:%S"),
        )
        hypothesis.status = "untested"
        hypothesis.validation_notes = result.reason
        return result

    precision = round(tp / (tp + fp), 3) if (tp + fp) > 0 else None
    recall = round(tp / (tp + fn), 3) if (tp + fn) > 0 else None
    baseline_hit_rate = round(positive_outcomes / outcomes_seen, 3)   # naive "always guess majority class"
    hypothesis_hit_rate = round((tp + tn) / outcomes_seen, 3)
    improvement = round(hypothesis_hit_rate - max(baseline_hit_rate, 1 - baseline_hit_rate), 3)

    # A hypothesis is only VALIDATED if it beats the naive baseline by a
    # meaningful margin (not just noise) AND precision is genuinely above
    # chance for its own predicted-positive rate.
    if improvement > 0.05 and precision is not None and precision > baseline_hit_rate + 0.05:
        verdict = "VALIDATED"
        reason = (f"Beats naive baseline by {improvement:+.1%} (hit-rate {hypothesis_hit_rate:.1%} vs "
                  f"baseline {max(baseline_hit_rate, 1-baseline_hit_rate):.1%}), precision {precision:.1%}.")
    else:
        verdict = "REJECTED"
        reason = (f"Does not beat naive baseline meaningfully (improvement {improvement:+.1%}, "
                  f"precision {precision if precision is not None else 'N/A'}). "
                  f"No evidence this metric adds predictive value over guessing the majority outcome.")

    hypothesis.status = verdict.lower() if verdict != "INSUFFICIENT_DATA" else "untested"
    hypothesis.validation_notes = reason

    result = TestResult(
        hypothesis_name=hypothesis.name, sample_size=outcomes_seen,
        true_positives=tp, false_positives=fp, true_negatives=tn, false_negatives=fn,
        precision=precision, recall=recall, baseline_hit_rate=baseline_hit_rate,
        hypothesis_hit_rate=hypothesis_hit_rate, improvement_over_baseline=improvement,
        verdict=verdict, reason=reason, tested_at=time.strftime("%Y-%m-%d %H:%M:%S"),
    )
    return result


def log_result(hypothesis: Hypothesis, result: TestResult):
    """Appends a test-result to the versioned, reproducible research log
    (research_log.json) -- never overwrites prior entries, so the full
    history of what was tried and what happened is always available."""
    log = []
    if RESEARCH_LOG_PATH.exists():
        try:
            log = json.loads(RESEARCH_LOG_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            log = []
    log.append({
        "hypothesis_name": hypothesis.name, "description": hypothesis.description,
        "predicts": hypothesis.predicts, "status": hypothesis.status,
        "result": {
            "sample_size": result.sample_size, "precision": result.precision, "recall": result.recall,
            "baseline_hit_rate": result.baseline_hit_rate, "hypothesis_hit_rate": result.hypothesis_hit_rate,
            "improvement_over_baseline": result.improvement_over_baseline,
            "verdict": result.verdict, "reason": result.reason, "tested_at": result.tested_at,
        },
    })
    RESEARCH_LOG_PATH.write_text(json.dumps(log, indent=2))
