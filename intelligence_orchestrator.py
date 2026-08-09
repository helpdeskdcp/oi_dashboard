"""
intelligence_orchestrator.py -- Milestone 13, Phase 1: Intelligence
Orchestrator. Combines already-computed, already-tested outputs from
this project's REAL existing engines into one MarketIntelligenceSnapshot
per symbol -- a read-only aggregation layer, not a new prediction model.

NOTE on file location and "existing engines": the Milestone 13 brief
named "backend/engines/probability_engine.py", "backend/engines/
oi_engine.py", "backend/engines/volume_engine.py", "backend/engines/
greeks_engine.py", "backend/engines/mathematics_engine.py" as the
engines to wrap. Verified before writing any code: no "backend/"
directory exists anywhere in this repository, and of those five names,
only "oi_engine.py" exists (at the repo root, not under any "engines/"
folder). This module wraps the REAL modules that actually implement
each concept instead, all at the repo root, matching this project's
one established convention:

- OI engine        -> oi_engine.py (generate_signal, detect_bias,
                       oi_walls, compute_trend_meter, BIAS_LEAN)
- Probability/
  institutional     -> sr_probability_engine.py
                       (compute_institutional_entry_score)
- Volume            -> no dedicated module exists; this orchestrator
                       derives a volume_score from the same near-ATM
                       ce_vol/pe_vol fields already returned by
                       market_data.get_snapshot() (see
                       _volume_and_liquidity() below) -- real data,
                       simply not behind a separately-named module.
- Greeks            -> the ATM/near-ATM delta oi_engine.generate_signal()
                       itself already resolved (its own "delta_used"/
                       "delta_source" fields) is reused directly, rather
                       than a second, independent Black-Scholes call --
                       generate_signal() is the SAME function that
                       decided the delta in the first place, so re-
                       deriving it a second, separate way would risk
                       disagreeing with the very signal this snapshot
                       is summarizing. greeks.py's own
                       black_scholes_greeks() exists and is real, but
                       needs an expiry_date this read-only snapshot
                       layer does not have on hand (option-chain expiry
                       resolution lives in app.py's broker-facing
                       AngelOneFetcher, which this module never imports
                       -- see "Safety" below).
- Mathematics       -> no dedicated module exists; the DETERMINISTIC
                       AGGREGATION step in build_snapshot() below (a
                       plain, documented arithmetic combination of the
                       other real sub-scores -- no ML, no external
                       model) is this orchestrator's own "mathematics"
                       layer, in the same spirit as oi_engine.py's own
                       compute_trend_meter()/compute_new_trend_meter()
                       composite-score functions.

Safety (Milestone 13, Phase 1 constraints):
- Pure read-only. The only functions called anywhere in this module are
  agents.trading_intelligence.market_data.get_snapshot() ("aggregated
  from already-stored data only" -- its own docstring), data_access.
  load_candles()/latest_market_structure() (already-archived files/
  rows), and oi_engine.py/sr_probability_engine.py's own pure
  functions (verified, in agents/shadow_mode/observer.py's own prior
  audit, to contain zero DB writes). Nothing here ever calls a broker,
  places an order, or writes to any table.
- No scheduler wiring, no background thread, no automatic trigger --
  build_snapshot() is called only by the GET /api/intelligence/snapshot
  route (app.py) and by tests.
- Deterministic: every score below is a documented, reproducible
  arithmetic function of the inputs -- calling build_snapshot() twice
  with identical underlying data returns identical results.
"""
import statistics

from oi_engine import BIAS_LEAN, compute_trend_meter, detect_bias, generate_signal, oi_walls
from sr_probability_engine import compute_institutional_entry_score

from agents.trading_intelligence import data_access, market_data

DEFAULT_TIMEFRAME = "3m"

# Reference near-ATM total volume (ce_vol + pe_vol summed across every
# strike market_data.get_snapshot() returns, which is already a bounded
# near-ATM window -- see wanted_strikes()/STRIKES_EACH_SIDE in app.py)
# treated as "fully liquid" (liquidity_score caps at 1.0 here and
# above). A documented, reasonable Phase 1 constant -- not empirically
# calibrated per symbol, the same honestly-flagged-heuristic status
# oi_engine.py's own HIGH_VOLATILITY_ATR_PCT constant carries.
LIQUID_VOLUME_REFERENCE = 50_000


def _price_trend_pct(candles) -> float | None:
    """Same cheap momentum proxy agents/shadow_mode/observer.py's own
    _price_trend_pct() already uses -- reimplemented locally rather
    than importing that module's private function, for the same
    "keep this decoupled from another package's internals" reason its
    own docstring gives."""
    if candles is None or candles.empty or len(candles) < 6:
        return None
    recent = candles.tail(6)
    start, end = recent.iloc[0]["close"], recent.iloc[-1]["close"]
    if not start:
        return None
    return round((end - start) / start * 100, 4)


def _normalize_bias(zone: str) -> str:
    """compute_trend_meter()'s own 7-value "zone" (STRONG BULLISH ..
    STRONG BEARISH) collapsed to the 3-value vocabulary this
    orchestrator's output model requires."""
    if "BULLISH" in zone:
        return "BULLISH"
    if "BEARISH" in zone:
        return "BEARISH"
    return "NEUTRAL"


def _greeks_alignment(signal: dict) -> str:
    """Reuses generate_signal()'s OWN already-resolved delta
    ("delta_used"/"direction") rather than a second, independent
    Black-Scholes computation -- see module docstring."""
    delta_used = signal.get("delta_used")
    direction = signal.get("direction")
    if delta_used is None:
        return "UNAVAILABLE"
    if direction == "CE":
        return "BULLISH LEAN"
    if direction == "PE":
        return "BEARISH LEAN"
    return "NEUTRAL"


def _volume_and_liquidity(strikes) -> tuple[int, float]:
    """(volume_score 0-100, liquidity_score 0.0-1.0) from the real
    ce_vol/pe_vol already carried on the snapshot's near-ATM strikes --
    no separate "volume engine" module exists in this codebase (see
    module docstring), so this is the orchestrator's own small, honest,
    documented derivation from real data, not a fabricated number."""
    total_volume = sum((row.ce_vol or 0) + (row.pe_vol or 0) for row in strikes)
    liquidity_score = min(1.0, total_volume / LIQUID_VOLUME_REFERENCE)
    volume_score = round(liquidity_score * 100)
    return volume_score, liquidity_score


def _institutional_score(*, bias: str, signal: dict, market_structure: dict | None,
                          underlying: float | None, liquidity_score: float) -> int:
    """Wraps sr_probability_engine.compute_institutional_entry_score()
    with inputs derived entirely from data this orchestrator already
    has -- every argument that function can gracefully degrade on
    (price_structure, vwap_aligned, regime, liquidity_score) is passed
    honestly as None/unavailable when this layer doesn't have a real
    value to offer, rather than a fabricated guess. See the function's
    own docstring: "Score represents evidence QUALITY, not a
    statistical probability of profit.\""""
    market_structure = market_structure or {}
    vwap = market_structure.get("vwap")
    if vwap is None or underlying is None or bias == "NEUTRAL":
        vwap_aligned = None
    elif bias == "BULLISH":
        vwap_aligned = underlying > vwap
    else:
        vwap_aligned = underlying < vwap

    # BIAS_LEAN's own magnitude (max 20) as a 0-100 "how much OI
    # evidence backs this bias" proxy -- reuses oi_engine's own already-
    # computed lean rather than a second, separate wall-concentration
    # formula.
    oi_evidence_pct = min(100, abs(BIAS_LEAN.get(signal.get("_bias_raw", ""), 0)) * 5)

    target_points, sl_points = signal.get("target_points"), signal.get("sl_points")
    risk_reward_ok = bool(target_points and sl_points and target_points >= sl_points)

    result = compute_institutional_entry_score(
        price_structure=None,  # no swing-structure classifier available at this layer yet (Phase 1 gap, honestly left unavailable)
        oi_evidence_pct=oi_evidence_pct,
        vwap_aligned=vwap_aligned,
        regime=market_structure.get("regime"),
        premium_momentum_confirmed=bool(signal.get("dual_source_checked")),
        wall_cross_verified=bool(signal.get("wall_cross_verified")),
        liquidity_score=liquidity_score,
        risk_reward_ok=risk_reward_ok,
    )
    return result["score"]


def build_snapshot(symbol: str, *, timeframe: str = DEFAULT_TIMEFRAME):
    """The orchestrator's one entrypoint. Returns a
    intelligence_models.MarketIntelligenceSnapshot, or None if no
    market snapshot is available yet for `symbol` (never raises for a
    data-availability reason, matching every other reader in this
    framework -- agents/shadow_mode/observer.py's own
    observe_and_predict() holds to the identical contract)."""
    from intelligence_models import MarketIntelligenceSnapshot

    snapshot = market_data.get_snapshot(symbol)
    if not snapshot.available or not snapshot.strikes or snapshot.atm is None or snapshot.pcr is None:
        return None

    candles = data_access.load_candles(symbol, timeframe=timeframe)
    price_trend_pct = _price_trend_pct(candles)
    market_structure = data_access.latest_market_structure(symbol)

    bias_raw, note = detect_bias(
        snapshot.strikes, snapshot.atm, snapshot.pcr, price_trend_pct, snapshot.underlying_ltp, market_structure,
    )
    support, resistance = oi_walls(snapshot.strikes)
    signal = generate_signal(
        snapshot.strikes, snapshot.atm, bias_raw, note, snapshot.pcr, support, resistance,
        underlying=snapshot.underlying_ltp, market_structure=market_structure,
    )
    signal["_bias_raw"] = bias_raw  # internal-only, used by _institutional_score(); never exposed in the output model

    trend = compute_trend_meter(bias_raw, snapshot.pcr, signal, market_structure)
    bias = _normalize_bias(trend["zone"])
    confidence = trend["score"]

    oi_strength = signal.get("confidence") or 0
    volume_score, liquidity_score = _volume_and_liquidity(snapshot.strikes)
    greeks_alignment = _greeks_alignment(signal)
    institutional_score = _institutional_score(
        bias=bias, signal=signal, market_structure=market_structure,
        underlying=snapshot.underlying_ltp, liquidity_score=liquidity_score,
    )

    # Deterministic aggregation ("mathematics" layer, see module
    # docstring): the mean of the three primary sub-scores, rounded --
    # an explicit cross-engine consensus figure, not any single
    # engine's own "textbook" statistical probability.
    probability_score = round(statistics.mean([oi_strength, institutional_score, confidence]))

    return MarketIntelligenceSnapshot(
        symbol=symbol,
        bias=bias,
        confidence=confidence,
        oi_strength=oi_strength,
        probability_score=probability_score,
        volume_score=volume_score,
        greeks_alignment=greeks_alignment,
        institutional_score=institutional_score,
    )
