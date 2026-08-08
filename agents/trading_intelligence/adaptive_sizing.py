"""
agents/trading_intelligence/adaptive_sizing.py -- Milestone 11, Module
11.5: Adaptive Risk & Position Sizing.

Two real, evidence-based adjustments layered on top of position_sizing.
compute_quantity()'s existing "risk_pct" formula -- never a second,
parallel risk formula, and never fabricated confidence:

1. Setup-quality scaling: the SAME entry-time evidence Module 11.3's
   trade_quality.py already scores a CLOSED trade on -- regime alignment
   (Module 11.1), timeframe confirmation (Module 11.2), institutional
   backing -- computed here BEFORE a trade opens, to scale this trade's
   OWN size by how well-supported it is. A historical track-record check
   (bucketed the SAME way trade_quality.quality_tier() already buckets a
   closed trade's score) further scales size by how well past trades in
   that same tier actually performed -- honestly gated by a minimum
   sample size, exactly like every calibration surface in this project.

2. Streak-aware dampener: reduces size after a real losing streak, but
   "real" is defined by risk_engine.simulate_drawdown_distribution()'s
   own bootstrap of this engine's ACTUAL closed-trade history -- never a
   hand-picked "N consecutive losses" magic number. A streak only trips
   the dampener once its cumulative loss reaches a real percentile of
   what this engine's own track record shows a plausible bad run can look
   like (which itself is built from max_drawdown() per bootstrap trial --
   the same drawdown primitive, not a second implementation of it).

Architecture note: this module does NOT add a new position_sizing.
VALID_SIZING_MODES entry in position_sizing.py itself. That module is
explicitly generic (used by Exit Engine V4's backtest replay, with no
Trading-Intelligence-specific concepts) and its own docstring already
rejects folding structure/ATR-specific knowledge into it for exactly this
reason. Threading regime/timeframe/institutional context into a shared,
generic module -- and making it depend on this agents.trading_intelligence
package -- would be a worse dependency direction than the reverse (this
module already depends on position_sizing, never the other way around).
Instead, compute_adaptive_quantity() below CALLS position_sizing.
compute_quantity(sizing_mode="risk_pct") unchanged for the base quantity,
then scales it -- literally "scales the risk_pct mode's own quantity,"
matching the plan's own wording, without touching position_sizing.py at
all (zero risk to Exit Engine V4's backtest usage of that module).

Hard invariant, enforced in code (not just by multiplier tuning): every
multiplier here is bounded in (0, 1.0] -- this mode can only ever size
DOWN from the risk_pct baseline, never up, so the same max-loss guarantee
risk_pct mode already provides (qty * per_unit_risk <= capital *
risk_pct/100) provably still holds for the adaptive quantity. Verified by
a dedicated comparison test, not a smoke test.
"""
import dataclasses

from . import ti_store, trade_quality

MIN_SETUP_MULTIPLIER = 0.5
MAX_SETUP_MULTIPLIER = 1.0  # never above 1.0 -- see module docstring's hard invariant

TRACK_RECORD_MIN_SAMPLE = 5  # same "don't trust a stat below this sample size" gate CALIBRATION_MIN_SAMPLE uses
MIN_TRACK_RECORD_MULTIPLIER = 0.5
TRACK_RECORD_NEUTRAL_WIN_RATE = 50.0  # a bucket win rate AT or above this contributes no scaling

STREAK_DAMPENER_MIN_TRADES = 10  # honesty gate: need real history before the dampener can activate at all
STREAK_DAMPENER_TRIALS = 500
STREAK_DAMPENER_PERCENTILE = 75
STREAK_DAMPENER_FACTOR = 0.5


@dataclasses.dataclass
class AdaptiveSizingResult:
    qty: int
    base_qty: int  # what plain risk_pct sizing alone would have produced -- qty is never allowed to exceed this
    setup_strength: float | None
    setup_multiplier: float
    track_record_multiplier: float
    track_record_reason: str
    streak_multiplier: float
    streak_reason: str


def _setup_strength(*, regime=None, alignment=None, institutional_backed: bool | None = None) -> tuple:
    """Averages whichever of the three entry-time components are
    available -- excluded (never fabricated as a mid-point) when absent,
    the same discipline trade_quality.score()'s own setup_strength uses.
    Reuses trade_quality.REGIME_TREND_SCORE directly rather than a second,
    possibly-drifting copy of the same regime-to-score mapping."""
    components = {
        "regime": trade_quality.REGIME_TREND_SCORE.get(regime.trend_regime) if regime is not None else None,
        "timeframe": alignment.alignment_score if alignment is not None else None,
        "institutional": None if institutional_backed is None else (100.0 if institutional_backed else 0.0),
    }
    available = [v for v in components.values() if v is not None]
    if not available:
        return None, components
    return round(sum(available) / len(available), 1), components


def _setup_multiplier(setup_strength: float | None) -> float:
    """No evidence -> neutral 1.0 (never fabricated confidence, and never
    a reduction just because data was unavailable). Otherwise linear from
    MIN_SETUP_MULTIPLIER at setup_strength=0 to MAX_SETUP_MULTIPLIER(=1.0)
    at setup_strength=100."""
    if setup_strength is None:
        return 1.0
    return round(MIN_SETUP_MULTIPLIER + (setup_strength / 100.0) * (MAX_SETUP_MULTIPLIER - MIN_SETUP_MULTIPLIER), 4)


def _quality_tier_win_rates(*, symbol: str | None = None) -> dict:
    """{tier: {"sample_size", "wins", "win_rate"}} across every CLOSED
    trade with an available trade_quality.score(), bucketed by
    trade_quality.quality_tier(q.setup_strength) -- deliberately the
    SETUP-strength tier, not quality_tier(q.score). q.score already bakes
    in outcome_alignment (whether the trade's own confidence direction
    matched what happened), so tiering by q.score would mix real wins
    together with "correctly anticipated losses" in the same HIGH bucket
    -- a misleading basis for "how often did a setup this strong actually
    win." Tiering by q.setup_strength instead answers exactly the
    question position sizing needs: historically, when the ENTRY looked
    this strong, what fraction of those trades actually won?

    Deliberately self-contained rather than calling ai_trading_engine.
    calibration_report(dimension="quality_tier") (which already computes
    a similar breakdown, but tiered by q.score for a different, reporting
    purpose): ai_trading_engine.evaluate() is THIS module's own caller for
    the "adaptive" sizing mode, so calling back into it here would be a
    circular import."""
    trades = ti_store.list_closed_trades(symbol=symbol, limit=10_000)
    buckets: dict = {}
    for t in trades:
        q = trade_quality.score(t)
        if not q.available:
            continue
        tier = trade_quality.quality_tier(q.setup_strength)
        b = buckets.setdefault(tier, {"sample_size": 0, "wins": 0})
        b["sample_size"] += 1
        b["wins"] += 1 if (t.get("points") or 0) > 0 else 0
    for b in buckets.values():
        b["win_rate"] = round(b["wins"] / b["sample_size"] * 100, 1) if b["sample_size"] else None
    return buckets


def _track_record_multiplier(setup_strength: float | None, *, symbol: str | None = None,
                              min_sample: int = TRACK_RECORD_MIN_SAMPLE) -> tuple:
    """1.0 (neutral, no adjustment) whenever there isn't enough real
    history to trust a tier's own win rate -- never a guessed number
    standing in for real history, matching every calibration surface in
    this project."""
    if setup_strength is None:
        return 1.0, "no setup-strength evidence to look up a track record for"

    tier = trade_quality.quality_tier(setup_strength)
    bucket = _quality_tier_win_rates(symbol=symbol).get(tier)
    sample_size = bucket["sample_size"] if bucket else 0
    if sample_size < min_sample:
        return 1.0, f"insufficient {tier}-tier history ({sample_size} closed trade(s), need >= {min_sample}) -- no adjustment"

    win_rate = bucket["win_rate"]
    if win_rate >= TRACK_RECORD_NEUTRAL_WIN_RATE:
        return 1.0, f"{tier}-tier track record ({win_rate}% win rate across {sample_size} trade(s)) at or above neutral -- no reduction"

    scaled = MIN_TRACK_RECORD_MULTIPLIER + (win_rate / TRACK_RECORD_NEUTRAL_WIN_RATE) * (1.0 - MIN_TRACK_RECORD_MULTIPLIER)
    return round(max(MIN_TRACK_RECORD_MULTIPLIER, scaled), 4), (
        f"{tier}-tier track record is only {win_rate}% win rate across {sample_size} trade(s) -- reducing size"
    )


def _current_losing_streak(trades_newest_first: list) -> tuple:
    """Consecutive losses starting from the MOST RECENT closed trade,
    stopping at the first win (or the end of history). Returns
    (streak_length, cumulative_points_lost -- a negative number or 0.0)."""
    streak_len = 0
    cumulative_loss = 0.0
    for t in trades_newest_first:
        pts = t.get("points")
        if pts is None or pts >= 0:
            break
        streak_len += 1
        cumulative_loss += pts
    return streak_len, cumulative_loss


def _streak_dampener_multiplier(*, symbol: str | None = None, min_trades: int = STREAK_DAMPENER_MIN_TRADES,
                                 trials: int = STREAK_DAMPENER_TRIALS, percentile: float = STREAK_DAMPENER_PERCENTILE,
                                 factor: float = STREAK_DAMPENER_FACTOR, rng=None) -> tuple:
    """Trips only once the CURRENT losing streak's own cumulative loss has
    reached or exceeded the worse of two real, evidence-based thresholds
    computed from this engine's PRIOR closed-trade history (i.e. every
    closed trade before the streak began -- never including the streak's
    own rows, so the streak can't inflate its own baseline):

    - risk_engine.max_drawdown() on that prior history -- literally "has
      this streak already lost more than the worst drawdown this engine
      has ever actually experienced before."
    - risk_engine.simulate_drawdown_distribution()'s own `percentile`
      (itself built on max_drawdown() per bootstrap trial) on that same
      prior history -- a smoothing safety margin so a single small,
      thin-sample "worst ever" doesn't become a trivially-easy bar.

    Never a hand-picked "N consecutive losses" magic number, and never a
    new drawdown formula -- both thresholds come straight from
    risk_engine's own existing primitives. `rng`: pass a seeded
    random.Random for reproducible tests; production callers leave it
    None (the same optional-rng convention simulate_drawdown_distribution
    itself already establishes)."""
    from agents.risk_manager import risk_engine

    trades = ti_store.list_closed_trades(symbol=symbol, limit=500)  # newest first
    if len(trades) < min_trades:
        return 1.0, f"insufficient closed-trade history ({len(trades)}, need >= {min_trades}) -- dampener inactive"

    streak_len, streak_loss = _current_losing_streak(trades)
    if streak_len == 0:
        return 1.0, "no active losing streak"

    prior_trades = trades[streak_len:]  # everything before the current streak began, still newest-first
    if len(prior_trades) < min_trades:
        return 1.0, (
            f"insufficient prior history before the current streak ({len(prior_trades)}, need >= {min_trades}) "
            f"-- dampener inactive"
        )

    prior_points = [t.get("points") or 0.0 for t in reversed(prior_trades)]  # chronological for a real equity-curve read
    historical_worst = risk_engine.max_drawdown(prior_points)
    sim = risk_engine.simulate_drawdown_distribution(prior_points, trials=trials, percentile=percentile, rng=rng)
    threshold = max(historical_worst, sim["percentile"])
    if threshold <= 0:
        return 1.0, "no meaningful prior drawdown to compare against -- dampener inactive"

    if abs(streak_loss) >= threshold:
        return factor, (
            f"current {streak_len}-trade losing streak ({streak_loss:+.2f} pts) has reached this engine's own "
            f"worst-known drawdown range ({threshold:.2f} pts, from its prior real closed-trade history) -- "
            f"reducing size"
        )
    return 1.0, (
        f"current {streak_len}-trade losing streak ({streak_loss:+.2f} pts) is within this engine's own normal "
        f"drawdown range (threshold {threshold:.2f} pts)"
    )


def compute_adaptive_quantity(entry: float, initial_sl: float, *, capital: float, risk_pct: float,
                               symbol: str | None = None, regime=None, alignment=None,
                               institutional_backed: bool | None = None,
                               min_qty: int | None = None, max_qty: int | None = None, rng=None) -> AdaptiveSizingResult:
    """The full Module 11.5 sizing read. Never raises -- every input this
    reads degrades honestly to a neutral (1.0) multiplier when
    unavailable, the same contract every M11 module already holds to.

    `regime`/`alignment`/`institutional_backed`: pass these when the
    caller already computed them this cycle (the same dedup discipline
    paper_trading.enter_from_recommendation() already established for
    Module 11.3) -- this function never fetches them itself, since unlike
    that function it has no `snapshot`/`findings` of its own to fetch
    with; a caller with only symbol+direction should compute these via
    regime_profile.classify()/timeframe_confirmation.check()/
    trade_quality.institutional_backing() first."""
    import position_sizing
    base_qty = position_sizing.compute_quantity(
        entry, initial_sl, sizing_mode="risk_pct", capital=capital, risk_pct=risk_pct, min_qty=0,
    )

    setup_strength, _components = _setup_strength(regime=regime, alignment=alignment, institutional_backed=institutional_backed)
    setup_mult = _setup_multiplier(setup_strength)
    track_mult, track_reason = _track_record_multiplier(setup_strength, symbol=symbol)
    streak_mult, streak_reason = _streak_dampener_multiplier(symbol=symbol, rng=rng)

    scaled = int(base_qty * setup_mult * track_mult * streak_mult)
    qty = min(scaled, base_qty)  # hard invariant -- see module docstring

    if min_qty is not None:
        qty = max(qty, min_qty)
    # base_qty is this module's own absolute ceiling -- the risk_pct
    # max-loss bound it exists to guarantee -- so a caller-supplied
    # min_qty floor is only honored UP TO that ceiling, never past it
    # (unlike position_sizing.compute_quantity()'s own min_qty, which is
    # deliberately allowed to override its risk-based quantity; that
    # module makes no max-loss guarantee for this module to preserve).
    qty = min(qty, base_qty)
    if max_qty is not None:
        qty = min(qty, max_qty)
    qty = max(qty, 0)

    return AdaptiveSizingResult(
        qty=qty, base_qty=base_qty, setup_strength=setup_strength, setup_multiplier=setup_mult,
        track_record_multiplier=track_mult, track_record_reason=track_reason,
        streak_multiplier=streak_mult, streak_reason=streak_reason,
    )
