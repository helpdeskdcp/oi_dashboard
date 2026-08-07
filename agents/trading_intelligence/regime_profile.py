"""
agents/trading_intelligence/regime_profile.py -- Milestone 11, Module
11.1: Regime & Institutional Persistence Engine.

Milestone 10's own engine already reads market regime (market_structure.
classify_regime(), a single-indicator ADX-only 4-state classifier: TRENDING/
RANGING/TRANSITIONING/UNKNOWN, already consumed by oi_engine.generate_signal())
but never scores it as its own thing -- ai_trading_engine.py only folds the
regime string into a text sentence inside price_action_reasoning. This
module adds two REAL, additional dimensions on top of that same regime
read, using data already ingested (no new external feed, no fabrication):

1. Volatility regime -- HIGH/NORMAL/LOW, ranked against the SAME symbol's
   own recent atr_14 history (market_structure_snapshots, already written
   by app.py's live loop) -- the exact "percentile of its own recent
   range, never a universal threshold, honestly UNKNOWN below a minimum
   sample" convention strike_intelligence._iv_rank() already established
   for IV Rank. A second, independent read from trend regime (ADX measures
   trend STRENGTH, not range width -- a market can trend gently in a
   historically calm range, or chop violently in a historically wide one).

2. Institutional build-up persistence -- is the ATM strike's CE/PE
   build-up type (already computed every cycle by oi_engine.classify_buildup,
   see strikes.ce_signal/pe_signal) the SAME reading for several
   consecutive cycles, or did it just appear this cycle? A build-up that's
   held for multiple readings is a meaningfully different (stronger)
   signal than one that flickered in this cycle alone -- reuses
   data_access.recent_strike_history() (already built for M10's own IV
   Rank/Premium Momentum reads), never a second history-fetching
   mechanism.

Both classify_regime()'s existing 4-string return value and every existing
caller of it (oi_engine.py) are UNCHANGED -- this module wraps it, never
replaces it. RegimeProfile is a new, standalone, optional read: nothing in
Milestone 10's pipeline calls this module yet (wiring it into
ai_trading_engine.Recommendation is deliberately left for a later,
separate step -- see MILESTONE11_PLAN.md's own "implement one module at a
time" discipline).
"""
import dataclasses
import logging

from market_structure import classify_regime

from . import data_access, market_data

logger = logging.getLogger("oi_dashboard.trading_intelligence.regime_profile")

# Minimum real atr_14 readings (besides the current one) before a
# volatility-regime percentile is computed -- below this, UNKNOWN is the
# honest answer, never a guessed rank. Matches strike_intelligence's own
# IV_RANK_MIN_HISTORY convention (3), widened slightly to 5 here since
# ATR (a rolling-window derivative of price itself) is noisier cycle to
# cycle than a quoted IV reading.
VOLATILITY_RANK_MIN_HISTORY = 5

# How many CONSECUTIVE most-recent cycles the same build-up type has to
# appear in (including the current cycle's own already-stored reading)
# before it counts as "persistent" rather than a same-cycle flicker.
PERSISTENCE_MIN_CYCLES = 3

# How many prior cycles' history to scan when counting persistence -- a
# hard cap so a build-up that has held for a very long time still reports
# a bounded, sane count rather than scanning a symbol's entire history.
PERSISTENCE_LOOKBACK_LIMIT = 20

# Volatility-regime percentile bands -- top/bottom third of the symbol's
# own recent atr_14 range. Named (not inline) so the 66.7/33.3 split is a
# single documented decision, not a repeated magic literal -- the same
# "no magic constants" discipline the Milestone 10 final review applied
# after finding strike_intelligence._strength()'s own normalizer bug.
VOLATILITY_HIGH_PERCENTILE = 200 / 3  # top third
VOLATILITY_LOW_PERCENTILE = 100 / 3   # bottom third


@dataclasses.dataclass
class RegimeProfile:
    symbol: str
    trend_regime: str  # "TRENDING" | "RANGING" | "TRANSITIONING" | "UNKNOWN" -- unchanged, market_structure.classify_regime()
    adx: float | None
    volatility_regime: str  # "HIGH" | "NORMAL" | "LOW" | "UNKNOWN"
    volatility_percentile: float | None  # 0-100, current atr_14's percentile within its own recent history; None if UNKNOWN
    atm_strike: int | None  # which strike the persistence read below is for (None if no cycle/ATM available)
    ce_buildup_persistent: bool
    pe_buildup_persistent: bool
    ce_persistence_cycles: int  # consecutive most-recent cycles (>=1) sharing the CURRENT ce_signal, capped at PERSISTENCE_LOOKBACK_LIMIT
    pe_persistence_cycles: int


def _volatility_regime(current_atr: float | None, atr_history: list) -> tuple[str, float | None]:
    """0-100 percentile of `current_atr` within `atr_history` (its own
    recent readings, oldest/newest order irrelevant here -- only the set
    of values matters) -- HIGH top third, LOW bottom third, NORMAL
    middle third. ("UNKNOWN", None) below VOLATILITY_RANK_MIN_HISTORY real
    (not None, not <=0) readings, or if `current_atr` itself is missing --
    never a fabricated rank from insufficient data."""
    values = [v for v in atr_history if v is not None and v > 0]
    if current_atr is None or current_atr <= 0 or len(values) < VOLATILITY_RANK_MIN_HISTORY:
        return "UNKNOWN", None
    all_values = values + [current_atr]
    lo, hi = min(all_values), max(all_values)
    if hi == lo:
        return "NORMAL", 50.0
    percentile = round((current_atr - lo) / (hi - lo) * 100, 1)
    if percentile >= VOLATILITY_HIGH_PERCENTILE:
        return "HIGH", percentile
    if percentile <= VOLATILITY_LOW_PERCENTILE:
        return "LOW", percentile
    return "NORMAL", percentile


def _persistence(strike_history: list, signal_field: str) -> tuple[bool, int]:
    """`strike_history` is data_access.recent_strike_history()'s own
    newest-first shape, so strike_history[0] IS the current cycle's
    already-stored reading (this engine only ever reads what's already
    written, never mid-write -- see package __init__.py's own contract).
    Counts consecutive entries from the start sharing strike_history[0]'s
    own signal value, stopping at the first disagreement. A "Neutral"
    current signal is never counted as persistent -- there is no
    build-up direction to persist."""
    if not strike_history:
        return False, 0
    current_signal = strike_history[0].get(signal_field)
    if current_signal in (None, "Neutral"):
        return False, 0
    count = 0
    for reading in strike_history:
        if reading.get(signal_field) != current_signal:
            break
        count += 1
    return count >= PERSISTENCE_MIN_CYCLES, count


def classify(symbol: str, *, snapshot=None, market_structure: dict | None = None) -> RegimeProfile:
    """The full Module 11.1 read for one symbol. Never raises -- every
    input this function reads (market_structure_snapshots, strikes
    history) degrades honestly to UNKNOWN/False/0 when absent, the same
    contract every data-reading function in this package already holds
    to; there is no state this function can be called in that fabricates
    a regime or a persistence claim from insufficient evidence.

    `snapshot`/`market_structure`: pass these when the caller already has
    them this cycle (ai_trading_engine.evaluate() already computes both)
    to skip a second read -- the same dedup discipline
    institutional_intelligence.analyze() and ai_trading_engine.evaluate()
    already established in the Milestone 10 review pass. Standalone
    callers (every test here) leave both None and get fresh reads."""
    if market_structure is None:
        market_structure = data_access.latest_market_structure(symbol)
    if snapshot is None:
        snapshot = market_data.get_snapshot(symbol)

    adx = market_structure.get("adx") if market_structure else None
    trend_regime = classify_regime(adx)

    current_atr = market_structure.get("atr_14") if market_structure else None
    atr_history_rows = data_access.recent_market_structure(symbol, limit=VOLATILITY_RANK_MIN_HISTORY + 20)
    # index 0 of the history read is the SAME reading as `current_atr`
    # (both come from the latest stored row) -- excluded from the
    # comparison set so the current reading is never ranked against
    # itself twice.
    atr_history = [row.get("atr_14") for row in atr_history_rows[1:]]
    volatility_regime, volatility_percentile = _volatility_regime(current_atr, atr_history)
    if volatility_regime == "UNKNOWN":
        logger.debug(
            "regime_profile: volatility regime UNKNOWN for %s -- only %d usable atr_14 reading(s), need >= %d",
            symbol, len([v for v in atr_history if v is not None and v > 0]), VOLATILITY_RANK_MIN_HISTORY,
        )

    atm = snapshot.atm if snapshot is not None and snapshot.available else None
    ce_persistent, pe_persistent, ce_cycles, pe_cycles = False, False, 0, 0
    if atm is not None:
        strike_history = data_access.recent_strike_history(symbol, atm, limit=PERSISTENCE_LOOKBACK_LIMIT)
        ce_persistent, ce_cycles = _persistence(strike_history, "ce_signal")
        pe_persistent, pe_cycles = _persistence(strike_history, "pe_signal")

    return RegimeProfile(
        symbol=symbol, trend_regime=trend_regime, adx=adx,
        volatility_regime=volatility_regime, volatility_percentile=volatility_percentile,
        atm_strike=atm,
        ce_buildup_persistent=ce_persistent, pe_buildup_persistent=pe_persistent,
        ce_persistence_cycles=ce_cycles, pe_persistence_cycles=pe_cycles,
    )
