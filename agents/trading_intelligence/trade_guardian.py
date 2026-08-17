"""
agents/trading_intelligence/trade_guardian.py -- Smart Mythos Trade
Guardian (post-launch upgrade, SHADOW/ADVISORY ONLY).

Analyzes an ALREADY-EXISTING trade -- one the Administrator manually
entered after a Telegram signal -- and continuously answers: "given the
current market state, is the original trade thesis still valid, and
what is the mathematically realistic remaining target/risk?" This
module never generates a new signal (that remains ai_trading_engine.py's
job, untouched) and never opens, closes, or modifies a real position.

Deliberately broker-free, per this package's own hard rule (verified by
test_agents/trading_intelligence/test_safety.py's AST scan): every
input here is either the Administrator's own recorded plan or an
already-computed value from data_access.py/institutional_intelligence.py/
regime_profile.py -- the same "read what's already been safely ingested,
never open a second broker session" pattern every other module in this
package already follows. The one genuinely broker-touching read (the
LIVE position's current LTP/qty) is the CALLER's responsibility to fetch
via app.py's existing _get_position_monitor_angel()/get_open_positions()
(the same shared-session path /live-positions already uses) and pass in
here as a plain dict -- this module never imports AngelOneFetcher or
SmartConnect.

ORIGINAL PLAN vs SMART RECOMMENDATION (Section 1's explicit requirement):
the Administrator's original entry/SL/T1/T2/T3, once registered via
register_position(), is IMMUTABLE (trade_guardian_store.register_plan()
refuses to overwrite an existing row) and always reported alongside,
never replacing, this module's own Smart Target/Smart SL recommendation.

Every numeric estimate here comes from real, already-ingested data
(recent_cycles/recent_strike_history/market_structure, oi_engine.oi_walls())
-- never a fixed/assumed option delta (Greeks are frequently unavailable
for MCX commodities in this deployment, confirmed for NATURALGAS during
the manual reference analysis this module reproduces), and never a
fabricated statistical probability when the sample is too thin (matches
ai_trading_engine.calibration_report()'s own "insufficient history,
honestly say so" convention).
"""
import dataclasses
import datetime as dt

import oi_engine
from . import data_access, institutional_intelligence, market_data, regime_profile, trade_guardian_store

# ---------------------------------------------------------------------------
# Chosen, NOT calibrated thresholds -- documented here so a future
# calibration pass (once real trade_guardian_decision_log history exists)
# can replace them honestly, the same "guessed vs derived" distinction
# risk_filters.py's own module docstring already established for its own
# CE/PE_MIN_CONFIDENCE cutoffs.
# ---------------------------------------------------------------------------
SENSITIVITY_MIN_SAMPLES = 10          # same-day (underlying, premium) pairs needed to trust a sensitivity read
SENSITIVITY_LOOKBACK = 80             # cycles of recent_strike_history_with_underlying() to scan
BREAKOUT_CONFIRMATION_LOOKBACK = 3    # consecutive cycles required beyond a wall before calling it "confirmed"
TARGET_SESSION_STRETCH_MULTIPLE = 1.5   # required-move vs today's-own-range multiple -> WEAK (no wall crossed)
TARGET_MULTI_WALL_MULTIPLE = 2.5        # required-move vs today's-own-range multiple -> UNSUPPORTED (wall crossed)
GUARDIAN_BREAKEVEN_R_MULTIPLE = 0.75  # gain/risk ratio at which SL moves to breakeven
GUARDIAN_TRAIL_R_MULTIPLE = 1.5       # gain/risk ratio at which SL starts trailing
GUARDIAN_TRAIL_FACTOR = 0.5           # trail distance = this fraction of the original risk unit, at minimum
GUARDIAN_ATR_TRAIL_FLOOR_MULTIPLIER = 1.0  # trail distance floor, in ATR units, when ATR is available

HEALTH_TIERS = (
    # (min_score_inclusive, tier) -- sorted descending, first match wins.
    # A lower-bound-only scheme (rather than (lo, hi) pairs) so no real,
    # non-integer weighted-average score (e.g. 74.3) can ever fall into a
    # gap between two integer band edges.
    (90, "STRONG"), (75, "HEALTHY"), (60, "CAUTION"), (40, "WEAK"), (0, "CRITICAL"),
)
ACTIONS = ("HOLD", "HOLD WITH CAUTION", "TRAIL", "PROTECT PROFIT", "REDUCE RISK", "EXIT / THESIS INVALIDATED")
INSUFFICIENT_DATA_NOTE = "INSUFFICIENT DATA -- QUALITATIVE ASSESSMENT ONLY"


@dataclasses.dataclass
class TargetLevel:
    label: str            # "T1" | "T2" | "T3" | "Original" | "Breakout"
    premium: float
    required_underlying: float | None
    verdict: str           # "SUPPORTED" | "CONDITIONAL" | "WEAK" | "UNSUPPORTED" | INSUFFICIENT_DATA_NOTE
    reason: str


@dataclasses.dataclass
class GuardianResult:
    position_id: str
    state: str
    underlying_ltp: float | None
    current_premium: float | None
    smart_sl: float | None
    sl_action: str
    smart_target_low: float | None
    smart_target_high: float | None
    breakout_target: float | None
    targets: list           # list[TargetLevel]
    trade_health_score: float | None
    trade_health_tier: str
    action: str
    reason: str
    component_scores: dict
    data_quality: dict
    error: str | None = None


def _position_id(symbol: str, strike: float, direction: str, entry_timestamp: str) -> str:
    """Stable, human-readable key shared across all three trade_guardian_store
    tables and the Telegram dedup fingerprint -- built in exactly ONE
    place, never re-derived differently elsewhere."""
    return f"{symbol}_{strike}_{direction}_{entry_timestamp}"


def register_position(*, symbol: str, strike: float, direction: str, entry_price: float, quantity: int,
                       original_sl: float, original_t1: float, original_t2: float | None = None,
                       original_t3: float | None = None, entry_timestamp: str | None = None,
                       expiry: str | None = None, signal_reference: str | None = None,
                       registered_by: str | None = None) -> str:
    """Captures the Administrator's ORIGINAL trade plan once. IMMUTABLE
    afterward -- trade_guardian_store.register_plan() silently returns
    the existing position_id unchanged on a repeat call, never
    overwriting (Section 1's explicit requirement: never infer or
    overwrite the original SL/target from anything, including a later
    Guardian recommendation or a Telegram signal)."""
    if direction not in ("CE", "PE"):
        raise ValueError(f"direction must be 'CE' or 'PE', got {direction!r}")
    entry_timestamp = entry_timestamp or dt.datetime.now().isoformat()
    position_id = _position_id(symbol, strike, direction, entry_timestamp)
    trade_guardian_store.register_plan({
        "position_id": position_id, "symbol": symbol, "expiry": expiry, "strike": strike,
        "direction": direction, "entry_price": entry_price, "quantity": quantity,
        "original_sl": original_sl, "original_t1": original_t1, "original_t2": original_t2,
        "original_t3": original_t3, "entry_timestamp": entry_timestamp,
        "signal_reference": signal_reference, "registered_by": registered_by,
    })
    return position_id


def _empirical_sensitivity(symbol: str, strike: float, *, premium_field: str,
                            min_samples: int = SENSITIVITY_MIN_SAMPLES,
                            lookback: int = SENSITIVITY_LOOKBACK) -> tuple:
    """Real, observed SAME-DAY premium-vs-underlying co-movement for one
    strike -- median of point-to-point (delta premium / delta underlying)
    ratios across actually-recorded cycles today. NEVER a fixed/assumed
    option delta. Returns (ratio_per_point: float|None, sample_size: int)
    -- None ratio when sample_size < min_samples, an honest
    "not enough history yet" signal, never a fabricated number. A prior
    session's data is never mixed in (different volatility/time-to-expiry
    regime, not safely comparable)."""
    history = data_access.recent_strike_history_with_underlying(symbol, strike, limit=lookback)
    if not history:
        return None, 0
    today = (history[0].get("cycle_ts") or "")[:10]
    same_day = [h for h in history if (h.get("cycle_ts") or "")[:10] == today]
    same_day = list(reversed(same_day))  # oldest -> newest
    ratios = []
    for prev, cur in zip(same_day, same_day[1:]):
        prev_u, cur_u = prev.get("underlying_ltp"), cur.get("underlying_ltp")
        prev_p, cur_p = prev.get(premium_field), cur.get(premium_field)
        if None in (prev_u, cur_u, prev_p, cur_p):
            continue
        du = cur_u - prev_u
        if abs(du) < 0.05:  # too small a move to extract a meaningful ratio from
            continue
        ratios.append((cur_p - prev_p) / du)
    if len(ratios) < min_samples:
        return None, len(ratios)
    ratios.sort()
    n = len(ratios)
    median = ratios[n // 2] if n % 2 else (ratios[n // 2 - 1] + ratios[n // 2]) / 2
    return round(median, 4), len(ratios)


def _today_range(symbol: str) -> tuple:
    """(low, high) of underlying_ltp for today's own recorded cycles, or
    (None, None) if nothing logged yet today."""
    today = dt.datetime.now().strftime("%Y-%m-%d")
    cycles = [c for c in data_access.recent_cycles(symbol, limit=500) if (c.get("date") or c.get("ts", "")[:10]) == today]
    if not cycles:
        return None, None
    lows = [c["underlying_ltp"] for c in cycles if c.get("underlying_ltp") is not None]
    return (min(lows), max(lows)) if lows else (None, None)


def _nearest_wall(rows, current_underlying: float, direction: str):
    """The nearest OI-wall obstacle in the direction this position needs
    the underlying to move -- reuses oi_engine.oi_walls() directly
    (never a second wall-detection formula, per oi_engine.py's own
    "never duplicate this logic elsewhere" rule). CE (bullish): nearest
    heavy-CE-OI strike ABOVE current price. PE (bearish): nearest
    heavy-PE-OI strike BELOW current price."""
    support, resistance = oi_engine.oi_walls(rows)
    if direction == "CE":
        candidates = sorted((r for r in resistance if r.strike > current_underlying), key=lambda r: r.strike)
    else:
        candidates = sorted((r for r in support if r.strike < current_underlying), key=lambda r: -r.strike)
    return candidates[0] if candidates else None


def _required_underlying_level(current_underlying: float, current_premium: float, target_premium: float,
                                sensitivity: float | None) -> float | None:
    if not sensitivity:
        return None
    return round(current_underlying + (target_premium - current_premium) / sensitivity, 2)


def _feasibility_verdict(*, required_level: float | None, current_underlying: float, direction: str,
                          today_range: tuple, wall, sample_size: int, min_samples: int) -> tuple:
    """Returns (verdict, reason). Never fabricates a probability -- when
    the sensitivity sample is too thin, the verdict IS the honest
    INSUFFICIENT_DATA_NOTE, not a guess dressed up as SUPPORTED/WEAK."""
    if sample_size < min_samples or required_level is None:
        return INSUFFICIENT_DATA_NOTE, (
            f"only {sample_size} same-day (underlying, premium) reading pair(s) available "
            f"(need >= {min_samples}) -- cannot honestly estimate the required underlying move"
        )
    lo, hi = today_range
    today_span = (hi - lo) if (lo is not None and hi is not None) else None
    distance = abs(required_level - current_underlying)
    beyond_wall = wall is not None and (
        (direction == "CE" and required_level > wall.strike) or (direction == "PE" and required_level < wall.strike)
    )
    multiple = (distance / today_span) if today_span else None
    if beyond_wall:
        wall_oi = wall.ce_oi if direction == "CE" else wall.pe_oi
        if multiple is not None and multiple > TARGET_MULTI_WALL_MULTIPLE:
            return "UNSUPPORTED", (
                f"requires underlying ~{required_level:g}, beyond the {wall.strike:g} OI wall "
                f"({wall_oi:,.0f} OI) and {multiple:.1f}x today's own range ({lo:g}-{hi:g}) -- "
                f"no evidence supports a move this large in one session"
            )
        return "CONDITIONAL", (
            f"requires underlying ~{required_level:g}, beyond the {wall.strike:g} OI wall "
            f"({wall_oi:,.0f} OI) -- only supported if that wall shows genuine breakout confirmation, "
            f"not merely a touch"
        )
    if multiple is not None and multiple > TARGET_SESSION_STRETCH_MULTIPLE:
        return "WEAK", (
            f"requires underlying ~{required_level:g}, {multiple:.1f}x today's own range ({lo:g}-{hi:g}) -- "
            f"a real stretch even without crossing a major wall"
        )
    return "SUPPORTED", (
        f"requires underlying ~{required_level:g}, within today's own observed range/momentum "
        f"and no major OI wall in the way"
    )


def _breakout_confirmed(symbol: str, wall_strike: float, direction: str,
                         *, lookback: int = BREAKOUT_CONFIRMATION_LOOKBACK) -> tuple:
    """A price touching resistance is NOT a breakout (Section on breakout
    logic). Requires the underlying to have HELD beyond the wall for
    `lookback` consecutive recent cycles (not a single tick) AND the
    wall's own OI to not be actively growing against the move (a proxy
    for "no fresh resistance being defended"). Returns (confirmed: bool,
    reason: str)."""
    cycles = data_access.recent_cycles(symbol, limit=lookback)
    if len(cycles) < lookback:
        return False, f"insufficient recent history ({len(cycles)}/{lookback} cycles) to confirm breakout"
    cycles = list(reversed(cycles))  # oldest -> newest
    beyond_all = all(
        (c["underlying_ltp"] > wall_strike if direction == "CE" else c["underlying_ltp"] < wall_strike)
        for c in cycles if c.get("underlying_ltp") is not None
    )
    if not beyond_all:
        return False, f"underlying has not held beyond {wall_strike:g} for {lookback} consecutive cycles yet"
    oi_field = "ce_oi" if direction == "CE" else "pe_oi"
    wall_history = data_access.recent_strike_history(symbol, wall_strike, limit=lookback)
    oi_values = [h.get(oi_field) for h in wall_history if h.get(oi_field) is not None]  # newest-first
    if len(oi_values) >= 2 and oi_values[0] > oi_values[-1]:
        return False, f"{wall_strike:g} OI is still rising against the move -- resistance not confirmed broken"
    return True, f"underlying has held beyond {wall_strike:g} for {lookback} cycles with no fresh OI resistance building"


def _dynamic_sl(*, direction: str, entry_price: float, original_sl: float, current_premium: float,
                 atr_14: float | None = None) -> tuple:
    """NEVER widens the original SL -- structurally enforced by the
    max()/min() clamp at the end of every branch, not just a documented
    convention. Mirrors virtual_trailing.py's own tiered breakeven/trail
    PATTERN (never its literal functions, which are coupled to
    ti_paper_trades's own schema) -- same invariant, same tier shape,
    generalized to any Guardian-tracked position. Returns
    (smart_sl, action, reason) where action is one of KEEP/BREAKEVEN/TRAIL
    -- WIDEN is not a value this function can ever return."""
    is_ce = direction == "CE"
    gain = (current_premium - entry_price) if is_ce else (entry_price - current_premium)
    risk = abs(entry_price - original_sl)
    if risk <= 0:
        return original_sl, "KEEP", "original SL equals entry price -- no risk unit to measure gain against"
    r_multiple = gain / risk
    if r_multiple < GUARDIAN_BREAKEVEN_R_MULTIPLE:
        return original_sl, "KEEP", f"gain is {r_multiple:.2f}R -- below the {GUARDIAN_BREAKEVEN_R_MULTIPLE}R breakeven threshold"
    if r_multiple < GUARDIAN_TRAIL_R_MULTIPLE:
        smart_sl = entry_price
        action, reason = "BREAKEVEN", f"gain reached {r_multiple:.2f}R -- move SL to breakeven to remove downside risk"
    else:
        trail_distance = max(risk * GUARDIAN_TRAIL_FACTOR, (atr_14 or 0) * GUARDIAN_ATR_TRAIL_FLOOR_MULTIPLIER)
        smart_sl = (current_premium - trail_distance) if is_ce else (current_premium + trail_distance)
        action, reason = "TRAIL", f"gain reached {r_multiple:.2f}R -- trailing SL to lock in progress"
    smart_sl = max(smart_sl, original_sl) if is_ce else min(smart_sl, original_sl)
    return round(smart_sl, 2), action, reason


def _score_component(value, *, good_range, reason_ok, reason_missing):
    """Shared 0-100 clamp + honest-missing helper for the simpler
    component scores below -- avoids repeating the same None-check/
    clamp boilerplate five times."""
    if value is None:
        return None, reason_missing
    lo, hi = good_range
    score = max(0.0, min(100.0, (value - lo) / (hi - lo) * 100.0)) if hi != lo else 50.0
    return round(score, 1), reason_ok


def _trend_component(recent_cycles: list, direction: str) -> tuple:
    vals = [c["underlying_ltp"] for c in recent_cycles if c.get("underlying_ltp") is not None]
    if len(vals) < 3:
        return None, "not enough recent cycles to read a trend"
    vals = list(reversed(vals))  # oldest -> newest
    net_move = vals[-1] - vals[0]
    aligned = net_move if direction == "CE" else -net_move
    score = 50.0 + max(-50.0, min(50.0, aligned * 20))  # each point of aligned move worth 20 score points, clamped
    reason = (
        f"underlying moved {net_move:+.2f} over the last {len(vals)} cycles -- "
        f"{'aligned with' if aligned > 0 else 'against' if aligned < 0 else 'flat vs'} the {direction} thesis"
    )
    return round(score, 1), reason


def _momentum_component(strike_history: list, premium_field: str) -> tuple:
    vals = [h.get(premium_field) for h in strike_history if h.get(premium_field) is not None]
    if len(vals) < 3:
        return None, "not enough recent premium readings to read momentum"
    vals = list(reversed(vals))
    net_move = vals[-1] - vals[0]
    pct = (net_move / vals[0] * 100) if vals[0] else 0
    score = 50.0 + max(-50.0, min(50.0, pct * 5))
    return round(score, 1), f"premium moved {pct:+.1f}% over the last {len(vals)} readings"


def _oi_component(findings: list, strike: float, direction: str) -> tuple:
    side = "CE" if direction == "CE" else "PE"
    matching = [f for f in findings if f.strike == strike and f.evidence.get("side") == side]
    if not matching:
        return 50.0, "no institutional-scale OI finding at this strike this cycle -- neutral"
    buying = any(f.pattern_type == "InstitutionalBuying" for f in matching)
    selling = any(f.pattern_type == "InstitutionalSelling" for f in matching)
    if direction == "CE":
        if buying:
            return 75.0, "institutional buying detected at this strike -- supportive of the CE thesis"
        if selling:
            return 25.0, "institutional selling detected at this strike -- works against the CE thesis"
    else:
        if selling:
            return 75.0, "institutional selling detected at this strike -- supportive of the PE thesis"
        if buying:
            return 25.0, "institutional buying detected at this strike -- works against the PE thesis"
    return 50.0, "institutional finding present but not directionally decisive -- neutral"


def _volume_component(strike_history: list, vol_field: str) -> tuple:
    vals = [h.get(vol_field) for h in strike_history if h.get(vol_field) is not None]
    if len(vals) < 3:
        return None, "not enough recent volume readings"
    vals = list(reversed(vals))
    rising = vals[-1] > vals[0]
    return (65.0 if rising else 45.0), f"volume {'rising' if rising else 'flat/declining'} over the last {len(vals)} readings"


def _vwap_component(market_structure: dict | None, underlying_ltp: float | None, direction: str) -> tuple:
    if not market_structure or market_structure.get("vwap") is None or underlying_ltp is None:
        return None, "VWAP not available in stored market structure"
    vwap = market_structure["vwap"]
    above = underlying_ltp > vwap
    aligned = above if direction == "CE" else (not above)
    return (70.0 if aligned else 30.0), (
        f"underlying is {'above' if above else 'below'} VWAP ({vwap:g}) -- "
        f"{'aligned with' if aligned else 'against'} the {direction} thesis"
    )


def _structure_component(market_structure: dict | None) -> tuple:
    if not market_structure or not market_structure.get("regime"):
        return None, "market structure regime not available"
    regime = market_structure["regime"]
    score = {"TRENDING": 65.0, "RANGING": 50.0, "TRANSITIONING": 40.0}.get(regime, 45.0)
    return score, f"market structure regime: {regime}"


def _volatility_component(regime: "regime_profile.RegimeProfile") -> tuple:
    mapping = {"HIGH": 55.0, "NORMAL": 65.0, "LOW": 40.0}
    if regime.volatility_regime not in mapping:
        return None, "volatility regime unknown -- insufficient ATR history"
    return mapping[regime.volatility_regime], f"volatility regime: {regime.volatility_regime}"


def _resistance_component(current_underlying: float, wall, today_range: tuple) -> tuple:
    if wall is None:
        return 70.0, "no major OI wall detected ahead in the required direction"
    lo, hi = today_range
    span = (hi - lo) if (lo is not None and hi is not None) else None
    distance = abs(wall.strike - current_underlying)
    if not span or span <= 0:
        return 50.0, f"nearest wall at {wall.strike:g}, today's range unavailable to judge proximity"
    ratio = distance / span
    score = max(0.0, min(100.0, ratio * 60))
    return round(score, 1), f"nearest wall at {wall.strike:g}, {ratio:.1f}x today's own range away"


def _reversal_component(recent_cycles: list, direction: str) -> tuple:
    trend_score, _ = _trend_component(recent_cycles, direction)
    if trend_score is None:
        return None, "not enough recent cycles to assess reversal risk"
    # reversal risk is the mirror of trend alignment: strongly against = high reversal risk = low score
    return round(100 - trend_score, 1), "derived from recent underlying trend vs the position's own direction"


def _target_feasibility_component(targets: list) -> tuple:
    scored = [t for t in targets if t.verdict != INSUFFICIENT_DATA_NOTE]
    if not scored:
        return None, "insufficient data for every evaluated target level"
    weights = {"SUPPORTED": 100.0, "CONDITIONAL": 60.0, "WEAK": 35.0, "UNSUPPORTED": 10.0}
    avg = sum(weights[t.verdict] for t in scored) / len(scored)
    return round(avg, 1), f"{sum(1 for t in scored if t.verdict == 'SUPPORTED')}/{len(scored)} evaluated target level(s) SUPPORTED"


def _trade_health(components: dict) -> tuple:
    """Transparent weighted score over only the components with a real
    (non-None) value this cycle -- weights renormalized among whatever
    is actually available, never a fabricated number for a component
    with insufficient data (Greeks/historical_similarity routinely
    degrade to None; the score still computes honestly from what IS
    known). Returns (score: float|None, tier: str)."""
    weights = {
        "trend": 1.0, "momentum": 1.0, "oi": 1.0, "volume": 0.75, "structure": 0.75, "vwap": 0.75,
        "volatility": 0.5, "greeks": 0.5, "resistance": 1.0, "target_feasibility": 1.5,
        "reversal_risk": 1.5, "historical_similarity": 0.5, "data_quality": 0.5,
    }
    available = {k: v for k, v in components.items() if v[0] is not None}
    if not available:
        return None, "CRITICAL"
    total_weight = sum(weights[k] for k in available)
    score = sum(available[k][0] * weights[k] for k in available) / total_weight
    tier = next(name for lo, name in HEALTH_TIERS if score >= lo)
    return round(score, 1), tier


def _action_from_state(*, health_tier: str, r_multiple: float, sl_action: str, invalidated: bool) -> str:
    if invalidated:
        return "EXIT / THESIS INVALIDATED"
    if health_tier == "CRITICAL":
        return "REDUCE RISK"
    if health_tier == "WEAK":
        return "REDUCE RISK" if r_multiple <= 0 else "HOLD WITH CAUTION"
    if health_tier == "CAUTION":
        return "HOLD WITH CAUTION"
    # HEALTHY or STRONG
    if sl_action == "TRAIL":
        return "TRAIL"
    if sl_action == "BREAKEVEN":
        return "PROTECT PROFIT"
    return "HOLD"


def evaluate_position(position_id: str, *, broker_position: dict | None = None) -> GuardianResult:
    """The full read-only evaluation for ONE registered position. Never
    raises -- any failure degrades to a GuardianResult with error set
    and action left conservative, matching every other advisory
    module's contract in this package.

    `broker_position`: the caller's already-fetched {ltp, net_qty, ...}
    dict for this position (via app.py's _get_position_monitor_angel()/
    get_open_positions() -- the ONLY safe broker-touching path). None
    means the broker state genuinely could not be confirmed this cycle
    -- the result's state becomes "UNKNOWN" and NO action recommendation
    is made that assumes a live quantity/LTP, per the explicit
    requirement that stale/uncertain broker state must result in no
    automatic action recommendation."""
    try:
        plan = trade_guardian_store.get_plan(position_id)
        if plan is None:
            return GuardianResult(
                position_id=position_id, state="UNKNOWN", underlying_ltp=None, current_premium=None,
                smart_sl=None, sl_action="KEEP", smart_target_low=None, smart_target_high=None,
                breakout_target=None, targets=[], trade_health_score=None, trade_health_tier="CRITICAL",
                action="HOLD", reason="no registered plan found for this position_id", component_scores={},
                data_quality={}, error="plan not found",
            )

        symbol, strike, direction = plan["symbol"], plan["strike"], plan["direction"]
        premium_field = "ce_ltp" if direction == "CE" else "pe_ltp"
        vol_field = "ce_vol" if direction == "CE" else "pe_vol"

        if broker_position is None:
            return GuardianResult(
                position_id=position_id, state="UNKNOWN", underlying_ltp=None, current_premium=None,
                smart_sl=plan["original_sl"], sl_action="KEEP", smart_target_low=None, smart_target_high=None,
                breakout_target=None, targets=[], trade_health_score=None, trade_health_tier="CRITICAL",
                action="HOLD", reason="POSITION STATE UNKNOWN -- broker position could not be confirmed this cycle",
                component_scores={}, data_quality={"broker_state": "unavailable"}, error=None,
            )

        snapshot = market_data.get_snapshot(symbol)
        if not snapshot.available:
            return GuardianResult(
                position_id=position_id, state="MONITORING", underlying_ltp=None,
                current_premium=broker_position.get("ltp"), smart_sl=plan["original_sl"], sl_action="KEEP",
                smart_target_low=None, smart_target_high=None, breakout_target=None, targets=[],
                trade_health_score=None, trade_health_tier="CRITICAL", action="HOLD",
                reason="no market data ingested yet for this symbol -- cannot evaluate", component_scores={},
                data_quality={"market_data": "unavailable"}, error=None,
            )

        rows = snapshot.strikes
        underlying_ltp = snapshot.underlying_ltp
        current_premium = broker_position.get("ltp")
        entry_price, original_sl = plan["entry_price"], plan["original_sl"]

        recent_cycles = data_access.recent_cycles(symbol, limit=20)
        strike_history = data_access.recent_strike_history(symbol, strike, limit=20)
        market_structure = data_access.latest_market_structure(symbol)
        today_range = _today_range(symbol)
        sensitivity, sample_size = _empirical_sensitivity(symbol, strike, premium_field=premium_field)
        regime = regime_profile.classify(symbol, snapshot=snapshot, market_structure=market_structure)
        findings = institutional_intelligence.analyze(symbol, snapshot=snapshot).get("findings", [])
        wall = _nearest_wall(rows, underlying_ltp, direction)

        candidate_targets = [
            ("T1", plan.get("original_t1")), ("T2", plan.get("original_t2")), ("T3", plan.get("original_t3")),
        ]
        targets = []
        for label, premium in candidate_targets:
            if premium is None:
                continue
            required = _required_underlying_level(underlying_ltp, current_premium or plan["entry_price"], premium, sensitivity)
            verdict, reason = _feasibility_verdict(
                required_level=required, current_underlying=underlying_ltp, direction=direction,
                today_range=today_range, wall=wall, sample_size=sample_size, min_samples=SENSITIVITY_MIN_SAMPLES,
            )
            targets.append(TargetLevel(label=label, premium=premium, required_underlying=required, verdict=verdict, reason=reason))

        breakout_target = None
        if wall is not None and targets:
            confirmed, breakout_reason = _breakout_confirmed(symbol, wall.strike, direction)
            if confirmed:
                for t in targets:
                    if t.verdict == "CONDITIONAL":
                        t.verdict, t.reason = "SUPPORTED", f"{t.reason} -- UPGRADED: {breakout_reason}"
                breakout_target = wall.strike

        supported = [t for t in targets if t.verdict in ("SUPPORTED", "CONDITIONAL") and t.premium is not None]
        smart_low = min((t.premium for t in supported), default=None)
        smart_high = max((t.premium for t in supported if t.verdict == "SUPPORTED"), default=smart_low)

        atr_14 = market_structure.get("atr_14") if market_structure else None
        smart_sl, sl_action, sl_reason = _dynamic_sl(
            direction=direction, entry_price=entry_price, original_sl=original_sl,
            current_premium=current_premium or entry_price, atr_14=atr_14,
        )

        lo, hi = today_range
        invalidated = False
        if lo is not None:
            invalidated = (underlying_ltp < lo) if direction == "CE" else (underlying_ltp > hi)

        components = {
            "trend": _trend_component(recent_cycles, direction),
            "momentum": _momentum_component(strike_history, premium_field),
            "oi": _oi_component(findings, strike, direction),
            "volume": _volume_component(strike_history, vol_field),
            "structure": _structure_component(market_structure),
            "vwap": _vwap_component(market_structure, underlying_ltp, direction),
            "volatility": _volatility_component(regime),
            "greeks": (None, "Greeks unavailable in stored data for this instrument/strike"),
            "resistance": _resistance_component(underlying_ltp, wall, today_range),
            "target_feasibility": _target_feasibility_component(targets),
            "reversal_risk": _reversal_component(recent_cycles, direction),
            "historical_similarity": (None, "pattern-memory not yet built (deferred -- see architecture assessment)"),
            "data_quality": (
                (100.0 if (sample_size >= SENSITIVITY_MIN_SAMPLES and market_structure) else 50.0),
                f"{sample_size} same-day sensitivity sample(s), market_structure {'available' if market_structure else 'unavailable'}",
            ),
        }
        health_score, health_tier = _trade_health(components)

        risk = abs(entry_price - original_sl)
        gain = (current_premium - entry_price) if direction == "CE" else (entry_price - current_premium)
        r_multiple = (gain / risk) if risk else 0.0
        action = _action_from_state(health_tier=health_tier, r_multiple=r_multiple, sl_action=sl_action, invalidated=invalidated)

        reason_parts = [sl_reason]
        if wall is not None:
            reason_parts.append(f"nearest wall {wall.strike:g}")
        if invalidated:
            reason_parts.append(f"underlying broke today's {'low' if direction == 'CE' else 'high'} against the thesis")
        reason = "; ".join(reason_parts)

        result = GuardianResult(
            position_id=position_id, state=("THESIS_INVALIDATED" if invalidated else "MONITORING"),
            underlying_ltp=underlying_ltp, current_premium=current_premium, smart_sl=smart_sl, sl_action=sl_action,
            smart_target_low=smart_low, smart_target_high=smart_high, breakout_target=breakout_target,
            targets=targets, trade_health_score=health_score, trade_health_tier=health_tier, action=action,
            reason=reason, component_scores={k: {"score": v[0], "reason": v[1]} for k, v in components.items()},
            data_quality={"sensitivity_samples": sample_size, "market_structure_available": bool(market_structure)},
            error=None,
        )
    except Exception as e:  # noqa: BLE001 -- advisory module, must never raise to its caller
        result = GuardianResult(
            position_id=position_id, state="UNKNOWN", underlying_ltp=None, current_premium=None, smart_sl=None,
            sl_action="KEEP", smart_target_low=None, smart_target_high=None, breakout_target=None, targets=[],
            trade_health_score=None, trade_health_tier="CRITICAL", action="HOLD",
            reason="evaluation failed -- defaulting to HOLD, no action recommendation made", component_scores={},
            data_quality={}, error=str(e),
        )

    _persist(result)
    return result


def _persist(result: GuardianResult) -> None:
    try:
        trade_guardian_store.upsert_state({
            "position_id": result.position_id, "state": result.state, "smart_sl": result.smart_sl,
            "smart_target_low": result.smart_target_low, "smart_target_high": result.smart_target_high,
            "breakout_target": result.breakout_target, "trade_health_score": result.trade_health_score,
            "trade_health_tier": result.trade_health_tier, "action": result.action, "reason": result.reason,
        })
        trade_guardian_store.log_decision({
            "position_id": result.position_id, "state": result.state, "underlying_ltp": result.underlying_ltp,
            "current_premium": result.current_premium, "smart_sl": result.smart_sl,
            "smart_target_low": result.smart_target_low, "smart_target_high": result.smart_target_high,
            "breakout_target": result.breakout_target, "trade_health_score": result.trade_health_score,
            "trade_health_tier": result.trade_health_tier, "action": result.action, "reason": result.reason,
            "component_scores": result.component_scores,
            "target_feasibility": {t.label: {"premium": t.premium, "verdict": t.verdict, "reason": t.reason} for t in result.targets},
            "data_quality": result.data_quality, "error": result.error,
        })
    except Exception:
        pass  # persistence failure must never break the evaluation result itself


def run_trade_guardian_cycle(broker_positions: list) -> list:
    """Evaluates every registered position. `broker_positions` is the
    caller's already-fetched list from get_open_positions() (app.py's
    job -- this function never calls the broker itself). Matches each
    registered plan to its live position by (symbol, strike, direction)
    prefix on the broker's own trading-symbol, the same match_symbol_prefix
    convention app.py's /live-positions route already uses; a plan with
    no matching broker position this cycle is evaluated with
    broker_position=None (POSITION STATE UNKNOWN, no action recommendation)
    rather than assumed closed or guessed at."""
    results = []
    for plan in trade_guardian_store.list_plans():
        match = next(
            (p for p in (broker_positions or [])
             if plan["symbol"] in (p.get("symbol") or "") and str(plan["strike"]) in (p.get("symbol") or "")
             and plan["direction"] in (p.get("symbol") or "")),
            None,
        )
        results.append(evaluate_position(plan["position_id"], broker_position=match))
    return results


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Smart Mythos Trade Guardian -- register an Administrator's trade plan and/or "
                     "run a read-only shadow evaluation against it. SHADOW/ADVISORY ONLY -- never "
                     "places, modifies, or cancels a broker order."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    reg = sub.add_parser("register", help="Register a new original trade plan (immutable once registered)")
    reg.add_argument("--symbol", required=True)
    reg.add_argument("--strike", type=float, required=True)
    reg.add_argument("--direction", choices=["CE", "PE"], required=True)
    reg.add_argument("--entry", type=float, required=True, dest="entry_price")
    reg.add_argument("--qty", type=int, required=True, dest="quantity")
    reg.add_argument("--sl", type=float, required=True, dest="original_sl")
    reg.add_argument("--t1", type=float, required=True, dest="original_t1")
    reg.add_argument("--t2", type=float, dest="original_t2")
    reg.add_argument("--t3", type=float, dest="original_t3")
    reg.add_argument("--entry-time", dest="entry_timestamp", help="ISO timestamp, default: now")
    reg.add_argument("--expiry")
    reg.add_argument("--signal-ref", dest="signal_reference")

    ev = sub.add_parser("evaluate", help="Run a read-only evaluation for an already-registered position_id")
    ev.add_argument("--position-id", required=True)
    ev.add_argument("--current-ltp", type=float, required=True,
                     help="Current option premium (the caller's own already-fetched broker LTP -- "
                          "this CLI never calls the broker itself)")
    ev.add_argument("--net-qty", type=int, default=1)

    args = parser.parse_args()

    if args.cmd == "register":
        pid = register_position(
            symbol=args.symbol, strike=args.strike, direction=args.direction, entry_price=args.entry_price,
            quantity=args.quantity, original_sl=args.original_sl, original_t1=args.original_t1,
            original_t2=args.original_t2, original_t3=args.original_t3, entry_timestamp=args.entry_timestamp,
            expiry=args.expiry, signal_reference=args.signal_reference, registered_by="cli",
        )
        print(f"Registered (or already existed): {pid}")
    else:
        broker_position = {"ltp": args.current_ltp, "net_qty": args.net_qty}
        result = evaluate_position(args.position_id, broker_position=broker_position)
        print(f"State: {result.state}")
        print(f"Underlying: {result.underlying_ltp}   Current premium: {result.current_premium}")
        print(f"Smart SL: {result.smart_sl} ({result.sl_action})")
        print(f"Smart Target: {result.smart_target_low}-{result.smart_target_high}   Breakout Target: {result.breakout_target}")
        for t in result.targets:
            print(f"  {t.label} ({t.premium}): {t.verdict} -- {t.reason}")
        print(f"Trade Health: {result.trade_health_score} ({result.trade_health_tier})")
        print(f"Action: {result.action}")
        print(f"Reason: {result.reason}")
        if result.error:
            print(f"ERROR: {result.error}")
