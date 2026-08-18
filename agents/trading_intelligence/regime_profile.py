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
import datetime as dt
import logging

import sr_probability_engine
from market_structure import classify_regime
from oi_engine import net_oi_buildup_lean

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
    never a fabricated rank from insufficient data.

    Phase 7 validation note: the percentile-of-range core (`values +
    [current], (current-lo)/(hi-lo)*100`) is deliberately the SAME shape
    as strike_intelligence._iv_rank(), a genuine parallel implementation
    rather than an accidental duplicate -- not imported/reused directly
    because this function additionally bands the raw percentile into
    HIGH/NORMAL/LOW and uses its own, separately-justified minimum-
    history threshold (see VOLATILITY_RANK_MIN_HISTORY above). Flagged
    explicitly so a future change to one formula's edge-case handling
    (e.g. the `hi == lo` fallback) is a conscious decision about whether
    to also update the other, not a silent miss."""
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
    current_id = market_structure.get("id") if market_structure else None
    atr_history_rows = data_access.recent_market_structure(symbol, limit=VOLATILITY_RANK_MIN_HISTORY + 20)
    # Exclude the row that IS `market_structure`'s own snapshot -- by its
    # real DB `id`, never by assuming it's positionally first in this
    # SEPARATE read (Phase 7 validation fix: `market_structure` and
    # `atr_history_rows` are two independent, non-atomic reads; if a
    # concurrent writer -- app.py's own background market-structure
    # loop -- inserts a new row between them, index 0 here can be that
    # NEW row, not a duplicate of `current_atr` at all. Slicing off
    # index 0 unconditionally would then silently drop a real data point
    # while the actual duplicate, now at some other index, stayed in the
    # set uncorrected -- the exact self-comparison this exclusion exists
    # to prevent. Matching by id is immune to that race regardless of
    # where the duplicate row lands.) `current_id is None` (no
    # `market_structure` was ever fetched, or a hand-built dict lacks it)
    # excludes nothing extra rather than guessing.
    atr_history = [row.get("atr_14") for row in atr_history_rows if current_id is None or row.get("id") != current_id]
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


# Post-launch upgrade: Market-Regime/Chop Detection layer. RISK GATE, NOT
# a second signal engine -- oi_engine.generate_signal() remains the ONLY
# place BUY CE/BUY PE/entry/target/SL are ever decided (see that module's
# own "Never duplicate this logic elsewhere" rule, and ai_trading_engine.
# py's own module docstring). This layer only classifies the REGIME a
# signal fired in and, in a later non-shadow phase, would gate whether a
# fresh entry is allowed through -- it never invents a price, target, or
# direction of its own.
#
# Every input below reuses an already-existing, already-computed read:
# classify() above (trend/volatility regime), oi_engine.net_oi_buildup_
# lean() (OI-direction support), sr_probability_engine.classify_price_
# structure()/check_premium_momentum_confirmed()/compute_volume_
# expansion()/fake_breakout_filter() (the SAME breakout-confirmation
# primitives already built for the separate S/R Engine pipeline -- see
# telegram_notifier.py's own docstring for why that pipeline is
# disconnected from this engine's entries; reusing its functions here is
# NOT reconnecting that pipeline, only reusing its already-validated
# building blocks). The "near a level" tolerance (atr*0.3, falling back
# to 0.1% of level_price) is the SAME formula sr_probability_engine.
# evaluate_resistance()/evaluate_support() already use -- not a new
# arbitrary threshold.

MARKET_REGIME_STATES = (
    "TRENDING_BULLISH", "TRENDING_BEARISH", "RANGE_BOUND", "EXPIRY_CHOP",
    "BREAKOUT_PENDING", "BREAKDOWN_PENDING", "LOW_MOMENTUM", "NO_TRADE",
)
MCX_REGIME_STATES = (
    "MCX_TRENDING_BULLISH", "MCX_TRENDING_BEARISH", "MCX_RANGE_BOUND",
    "MCX_LOW_MOMENTUM", "MCX_BREAKOUT_PENDING", "MCX_BREAKDOWN_PENDING", "MCX_NO_TRADE",
)

TRADEABILITY_NO_TRADE = "NO_TRADE"
TRADEABILITY_WAIT = "WAIT"
TRADEABILITY_CE_CANDIDATE = "CE_CANDIDATE"
TRADEABILITY_PE_CANDIDATE = "PE_CANDIDATE"


@dataclasses.dataclass
class MarketRegimeAssessment:
    symbol: str
    market: str  # "NSE" | "MCX"
    regime: str  # one of MARKET_REGIME_STATES (NSE) or MCX_REGIME_STATES (MCX)
    tradeability: str  # TRADEABILITY_* above
    ai_confidence: int | None  # the SAME confidence generate_signal() already produced -- never a second scale
    reason: str
    breakout_override: bool  # True if a chop/range read was overridden by a confirmed breakout/breakdown
    is_expiry_day: bool  # NSE only -- always False for MCX


def _price_structure_for(symbol: str) -> str:
    """sr_probability_engine.classify_price_structure() needs a newest-
    unused, oldest-first list of {"ltp": float} underlying closes --
    reuses the SAME 3m candle archive ai_trading_engine.evaluate() already
    reads via data_access.load_candles() for its own price-trend read,
    never a second candle source."""
    candles = data_access.load_candles(symbol)
    if not candles:
        return "INSUFFICIENT_DATA"
    history = [{"ltp": c["close"]} for c in candles if c.get("close") is not None]
    return sr_probability_engine.classify_price_structure(history)


def _breakout_confirmation(symbol: str, *, direction: str, strike: int | None, rows: list,
                            underlying: float | None, market_structure: dict | None) -> tuple[bool, list]:
    """Reuses sr_probability_engine's own fake-breakout filter, fed with
    THIS symbol/strike's real recent history -- never a fabricated
    volume/premium/VWAP reading. Returns (passes, failed_checks)."""
    if strike is None or underlying is None:
        return False, ["insufficient data (no ATM strike or underlying price)"]

    history = data_access.recent_strike_history(symbol, strike, limit=15)
    field = "ce" if direction == "CE" else "pe"
    # recent_strike_history() is newest-first; premium-momentum/volume
    # helpers below expect oldest-first (they read history[-1] as "now").
    oldest_first = list(reversed(history))
    premium_history = [{"ltp": r.get(f"{field}_ltp")} for r in oldest_first]
    volume_history = [r.get(f"{field}_vol") for r in oldest_first[:-1]]
    current_volume = oldest_first[-1].get(f"{field}_vol") if oldest_first else None

    momentum_confirmed, _, _ = sr_probability_engine.check_premium_momentum_confirmed(premium_history)
    volume_expanded, _ = sr_probability_engine.compute_volume_expansion(volume_history, current_volume)

    latest_row = rows[0] if rows else None  # only used for signal fields below, per-strike, not price
    atm_row = next((r for r in rows if r.strike == strike), latest_row)
    ce_signal = atm_row.ce_signal if atm_row else "Neutral"
    pe_signal = atm_row.pe_signal if atm_row else "Neutral"
    lean = net_oi_buildup_lean(ce_signal, pe_signal)
    oi_supports_direction = (lean["overall"] == "BULLISH") if direction == "CE" else (lean["overall"] == "BEARISH")

    vwap = (market_structure or {}).get("vwap")
    vwap_aligned = None
    if vwap is not None and underlying is not None:
        vwap_aligned = (underlying >= vwap) if direction == "CE" else (underlying <= vwap)

    return sr_probability_engine.fake_breakout_filter(
        volume_expanded, oi_supports_direction, momentum_confirmed, vwap_aligned,
    )


def classify_market_regime(symbol: str, *, direction: str, confidence: int | None, rows: list,
                            atm: int | None, underlying: float | None, support: list,
                            resistance: list, market_structure: dict | None = None,
                            snapshot=None, expiry_date: dt.date | None = None, is_mcx: bool = False,
                            regime: "RegimeProfile | None" = None) -> MarketRegimeAssessment:
    """The Market-Regime/Chop Detection layer (post-launch upgrade). Called
    ONLY after oi_engine.generate_signal() has already produced an
    actionable BUY CE/BUY PE this cycle -- this function never runs
    against a HOLD/NO_TRADE cycle (there is no fresh entry to gate).

    NSE-specific EXPIRY_CHOP framing only applies when `is_mcx` is False
    AND `expiry_date` is today -- MCX commodities never get NSE expiry
    semantics (Requirement 4: "Do NOT apply NSE-specific expiry logic to
    Natural Gas/Crude Oil/Gold/Silver"), and a non-expiry NSE session
    with the same chop signature is classified RANGE_BOUND, not
    EXPIRY_CHOP.

    Market state is judged as CHOPPY when trend_regime is RANGING/
    TRANSITIONING AND the underlying's own recent price structure is
    MIXED (sr_probability_engine.classify_price_structure()) -- two
    independent reads (ADX-derived trend strength, and actual swing-
    high/swing-low structure) agreeing, not a single indicator. A chop
    read is overridden (BREAKOUT_PENDING/BREAKDOWN_PENDING -> the normal
    entry engine may proceed) exactly when sr_probability_engine.
    fake_breakout_filter() -- volume expansion + OI-direction support +
    premium momentum + VWAP alignment -- passes AND price is genuinely
    near a structural level (not floating mid-range).

    `support`/`resistance`: the SAME oi_engine.oi_walls(rows) return value
    every caller already has this cycle -- top-3-by-OI StrikeRow lists,
    ordered heaviest first (never a scalar price). The heaviest wall's own
    `.strike` is the structural level used below, matching _multi_targets()'s
    own `walls[0]`/`wall.strike` indexing exactly -- no second convention
    for "the" support/resistance price invented here."""
    if regime is None:
        regime = classify(symbol, snapshot=snapshot, market_structure=market_structure)

    is_expiry_day = (not is_mcx) and expiry_date is not None and expiry_date == dt.datetime.now().date()
    price_structure = _price_structure_for(symbol)
    is_choppy = regime.trend_regime in ("RANGING", "TRANSITIONING") and price_structure == "MIXED"

    atr = (market_structure or {}).get("atr_14")
    walls = resistance if direction == "CE" else support
    level_price = walls[0].strike if walls else None
    tolerance = (atr * 0.3) if atr else (max(1.0, level_price * 0.001) if level_price else None)
    near_level = (
        level_price is not None and underlying is not None and tolerance is not None
        and abs(underlying - level_price) <= tolerance
    )

    breakout_passes, failed_checks = False, ["not evaluated -- not near a structural level"]
    if near_level:
        breakout_passes, failed_checks = _breakout_confirmation(
            symbol, direction=direction, strike=atm, rows=rows, underlying=underlying,
            market_structure=market_structure,
        )

    trend_matches_direction = (
        (regime.trend_regime == "TRENDING" and price_structure == "HIGHER_HIGH_HIGHER_LOW" and direction == "CE")
        or (regime.trend_regime == "TRENDING" and price_structure == "LOWER_HIGH_LOWER_LOW" and direction == "PE")
    )
    tradeable = TRADEABILITY_CE_CANDIDATE if direction == "CE" else TRADEABILITY_PE_CANDIDATE
    pending_state = "BREAKOUT_PENDING" if direction == "CE" else "BREAKDOWN_PENDING"
    mcx_pending_state = "MCX_BREAKOUT_PENDING" if direction == "CE" else "MCX_BREAKDOWN_PENDING"
    trending_state = "TRENDING_BULLISH" if direction == "CE" else "TRENDING_BEARISH"
    mcx_trending_state = "MCX_TRENDING_BULLISH" if direction == "CE" else "MCX_TRENDING_BEARISH"

    if trend_matches_direction:
        regime_name = mcx_trending_state if is_mcx else trending_state
        return MarketRegimeAssessment(
            symbol=symbol, market="MCX" if is_mcx else "NSE", regime=regime_name, tradeability=tradeable,
            ai_confidence=confidence, breakout_override=False, is_expiry_day=is_expiry_day,
            reason=f"Trend regime TRENDING with {price_structure} price structure -- direction confirmed.",
        )

    if is_choppy:
        if near_level and breakout_passes:
            override_label = "EXPIRY_CHOP -> BREAKOUT CONFIRMED" if is_expiry_day and direction == "CE" else (
                "EXPIRY_CHOP -> BREAKDOWN CONFIRMED" if is_expiry_day else (
                    f"{'MCX_RANGE_BOUND' if is_mcx else 'RANGE_BOUND'} -> BREAKOUT CONFIRMED" if direction == "CE"
                    else f"{'MCX_RANGE_BOUND' if is_mcx else 'RANGE_BOUND'} -> BREAKDOWN CONFIRMED"
                )
            )
            regime_name = mcx_pending_state if is_mcx else pending_state
            return MarketRegimeAssessment(
                symbol=symbol, market="MCX" if is_mcx else "NSE", regime=regime_name, tradeability=tradeable,
                ai_confidence=confidence, breakout_override=True, is_expiry_day=is_expiry_day,
                reason=f"{override_label} -- volume/OI/premium-momentum/VWAP all confirm follow-through.",
            )
        base_regime = "EXPIRY_CHOP" if is_expiry_day else ("MCX_RANGE_BOUND" if is_mcx else "RANGE_BOUND")
        reason = (
            f"Price trapped near {level_price} with no confirmed breakout follow-through "
            f"({'; '.join(failed_checks)})." if near_level else
            "Price mid-range, not testing a structural level; ADX/price-structure show no directional edge."
        )
        return MarketRegimeAssessment(
            symbol=symbol, market="MCX" if is_mcx else "NSE", regime=base_regime,
            tradeability=TRADEABILITY_NO_TRADE, ai_confidence=confidence, breakout_override=False,
            is_expiry_day=is_expiry_day, reason=f"NO TRADE -- {base_regime}. {reason}",
        )

    if near_level and not breakout_passes:
        regime_name = mcx_pending_state if is_mcx else pending_state
        return MarketRegimeAssessment(
            symbol=symbol, market="MCX" if is_mcx else "NSE", regime=regime_name, tradeability=TRADEABILITY_WAIT,
            ai_confidence=confidence, breakout_override=False, is_expiry_day=is_expiry_day,
            reason=f"WAIT -- {regime_name}. Level tested but follow-through not confirmed "
                   f"({'; '.join(failed_checks)}).",
        )

    if regime.trend_regime == "UNKNOWN" or regime.adx is None:
        regime_name = "MCX_LOW_MOMENTUM" if is_mcx else "LOW_MOMENTUM"
        return MarketRegimeAssessment(
            symbol=symbol, market="MCX" if is_mcx else "NSE", regime=regime_name,
            tradeability=TRADEABILITY_NO_TRADE, ai_confidence=confidence, breakout_override=False,
            is_expiry_day=is_expiry_day, reason=f"NO TRADE -- {regime_name}. ADX/regime data not yet available.",
        )

    regime_name = "MCX_LOW_MOMENTUM" if is_mcx else "LOW_MOMENTUM"
    return MarketRegimeAssessment(
        symbol=symbol, market="MCX" if is_mcx else "NSE", regime=regime_name, tradeability=TRADEABILITY_NO_TRADE,
        ai_confidence=confidence, breakout_override=False, is_expiry_day=is_expiry_day,
        reason=f"NO TRADE -- {regime_name}. ADX weak and premium momentum insufficient.",
    )
