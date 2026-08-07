"""
agents/trading_intelligence/strike_intelligence.py -- Module 2b, per the
explicit follow-up request: "Milestone 10 madhye tumhi vaparat asलेल्या
Option Chain screen sarkhi AI Intelligence tayar kara" -- a per-strike
table, the same shape as the live Option Chain screen this repo already
has. Extended for the institutional-grade review pass (Priority 3):
Support/Resistance Strength (renamed from the original CE/PE Strength --
same computation, clearer name: heavy PE OI backs support, heavy CE OI
backs resistance, the same convention institutional_intelligence.py's
own oi_wall_findings() already uses), a graded OI Wall Score, Max Pain
Distance, Gamma/Delta Exposure, IV Rank, Premium Momentum, Probability
of ITM, and a composite AI Strike Score.

Every field is either read straight off the already-live StrikeRow, a
real closed-form options formula (Expected Move, Probability of ITM --
both textbook, not proprietary), or a small, honestly-scoped derived
score with its weights documented in place -- never a duplicate OI/PCR/
Greeks computation (those stay owned by oi_engine.py/greeks.py; see
market_data.py, which this module now shares its Greeks-filling logic
with via market_data.fill_missing_greeks(), and
institutional_intelligence.py, which this module reuses rather than
re-querying the database itself).

AI Buy/Sell Probability: an HONEST label for what this actually is --
oi_engine.net_oi_buildup_lean's own -2..+2 lean, linearly mapped to a
10-90% band (never claiming 0% or 100% certainty, the same clamping
philosophy oi_engine.generate_signal's own confidence score already
uses). This is a POSITIONING read (what the OI data says right now), a
DIFFERENT thing from ai_trading_engine.py's own `probability` field
(a genuinely calibrated, historical-trade-based number) and from THIS
module's own Probability of ITM (a risk-neutral options-pricing
probability) -- three distinct numbers, on purpose, never confused with
each other in this codebase.
"""
import dataclasses
import datetime as dt

from oi_engine import calc_max_pain, net_oi_buildup_lean, oi_walls

from . import data_access, market_data

IV_RANK_MIN_HISTORY = 3
PREMIUM_MOMENTUM_MIN_HISTORY = 3

# AI Strike Score weights -- transparent arithmetic, not a model. Each
# component is normalized to its own 0-100 scale first, THEN weighted,
# so no single input can dominate just because its raw units are larger.
# Weights sum to 1.0 by construction (checked below, not just by eye).
_SCORE_WEIGHTS = {"oi_wall": 0.30, "conviction": 0.25, "gamma_exposure": 0.20, "itm_contestedness": 0.25}
assert abs(sum(_SCORE_WEIGHTS.values()) - 1.0) < 1e-9, "_SCORE_WEIGHTS must sum to 1.0"

# 0-100: how much directional CONVICTION a build-up type carries for the
# AI Strike Score's own purposes -- Neutral is genuinely 0 here (no
# directional information at all to score). This is a DIFFERENT question
# from _STRENGTH_CONVICTION_MULT below (Support/Resistance Strength),
# where even a Neutral-signal strike still carries SOME strength from its
# raw OI share alone -- the two constants intentionally disagree on
# Neutral's value because they answer different questions, not because
# one of them is wrong.
_CONVICTION_POINTS = {"Long Buildup": 100, "Short Buildup": 100, "Short Covering": 50,
                       "Long Unwinding": 50, "Neutral": 0}

# Support/Resistance Strength (_strength(), below) weights and caps.
# share_pts = min(SHARE_CAP_PTS, oi_share_pct * SHARE_SCALE) -- a strike
# holding SHARE_CAP_PTS/SHARE_SCALE % of one side's total OI already maxes
# this term (35% by default: 70/2).
_STRENGTH_SHARE_CAP_PTS = 70.0
_STRENGTH_SHARE_SCALE = 2.0
# Fresh, same-direction positioning (Long/Short Buildup) counts full
# weight; fading positioning (Short Covering, Long Unwinding) counts
# partial weight; Neutral gets the mid-point default since raw OI still
# backs a level even without directional conviction.
_STRENGTH_CONVICTION_MULT = {"Long Buildup": 1.0, "Short Buildup": 1.0, "Short Covering": 0.6,
                              "Long Unwinding": 0.4, "Neutral": 0.5}
# The formula's mathematical ceiling: share_pts maxes at SHARE_CAP_PTS,
# the conviction factor maxes at (1 + max(mult)). Normalizing by this
# EXACT product (not an arbitrary divisor) is what makes _strength()'s
# 0-100 range genuinely achievable at its true extreme, rather than
# silently capping out below 100.
_STRENGTH_MAX_RAW = _STRENGTH_SHARE_CAP_PTS * (1 + max(_STRENGTH_CONVICTION_MULT.values()))


@dataclasses.dataclass
class StrikeIntelligence:
    strike: int
    is_oi_wall_support: bool
    is_oi_wall_resistance: bool
    oi_wall_score: float  # 0-100: this strike's (ce_oi+pe_oi) as a share of the whole chain's OI
    is_max_pain: bool
    max_pain_distance: float  # abs(strike - max_pain), in points
    ce_buildup_type: str
    pe_buildup_type: str
    net_lean: str  # "BULLISH" | "BEARISH" | "NEUTRAL"
    ai_buy_probability_pct: int  # CE-side positioning lean, 10-90 -- see module docstring
    ai_sell_probability_pct: int  # PE-side positioning lean, 10-90
    support_strength: int  # 0-100, was "pe_strength"
    resistance_strength: int  # 0-100, was "ce_strength"
    ce_gamma_exposure: float
    pe_gamma_exposure: float
    ce_delta_exposure: float
    pe_delta_exposure: float
    ce_prob_itm: float | None  # risk-neutral N(d2)/N(-d2), real Black-Scholes, None if not computable
    pe_prob_itm: float | None
    ce_iv_rank: float | None  # 0-100, this strike's CE IV vs its OWN recent history; None if <3 readings
    pe_iv_rank: float | None
    ce_premium_momentum: float | None  # % vs short-term average of recent premium readings
    pe_premium_momentum: float | None
    expected_move_pts: float | None
    ai_strike_score: int  # 0-100 composite -- see _SCORE_WEIGHTS
    ce_oi: int
    pe_oi: int
    ce_ltp: float
    pe_ltp: float


def expected_move(underlying: float | None, iv_percent: float | None, expiry_date: dt.date | None) -> float | None:
    """Standard 1-SD expected move by expiry: spot * IV * sqrt(T). None if
    any input is missing or IV is non-positive -- never a fabricated
    number from a degenerate input. Public (not module-private): reused
    by ai_trading_engine.py's own Recommendation.expected_move_pts field
    (Milestone 10's Priority-2 review) rather than a second copy of this
    formula living there."""
    if not underlying or not iv_percent or iv_percent <= 0 or not expiry_date:
        return None
    from greeks import time_to_expiry_years
    T = time_to_expiry_years(expiry_date)
    import math
    return round(underlying * (iv_percent / 100) * math.sqrt(T), 2)


def _prob_itm(underlying: float | None, strike: float, iv_percent: float | None,
              expiry_date: dt.date | None, option_type: str) -> float | None:
    """Real Black-Scholes N(d2)/N(-d2) -- reuses greeks.black_scholes_greeks
    (already extended with a prob_itm key this review pass) rather than a
    second implementation of the same formula."""
    if not underlying or not iv_percent or iv_percent <= 0 or not expiry_date:
        return None
    from greeks import black_scholes_greeks, time_to_expiry_years
    T = time_to_expiry_years(expiry_date)
    g = black_scholes_greeks(underlying, strike, T, iv_percent, option_type)
    return g.get("prob_itm") if g.get("valid") else None


def _iv_rank(current_iv: float | None, history_ivs: list) -> float | None:
    """0-100: where the CURRENT IV reading sits within its own recent
    range (min=0, max=100) -- a real percentile-of-range, not a
    fabricated number. None until at least IV_RANK_MIN_HISTORY real
    readings exist. 50.0 (honestly neutral, not a crash) if the history
    is perfectly flat (max==min -- e.g. very early in a symbol's own
    data collection)."""
    values = [v for v in history_ivs if v is not None and v > 0]
    if current_iv is None or current_iv <= 0 or len(values) < IV_RANK_MIN_HISTORY:
        return None
    all_values = values + [current_iv]
    lo, hi = min(all_values), max(all_values)
    if hi == lo:
        return 50.0
    return round((current_iv - lo) / (hi - lo) * 100, 1)


def _premium_momentum(strike_history: list, ltp_field: str) -> float | None:
    """Reuses sr_probability_engine.compute_premium_momentum -- the SAME
    "current premium vs. short-term average of recent readings" function
    scalping_engine.py/sr_engine_v3.py already use, never a second
    momentum formula. `strike_history` is data_access.recent_strike_history's
    own newest-first shape; compute_premium_momentum needs oldest-first
    with the CURRENT reading last, so this reverses it."""
    from sr_probability_engine import compute_premium_momentum
    oldest_first = list(reversed(strike_history))
    premium_history = [{"ltp": h.get(ltp_field)} for h in oldest_first]
    if len(premium_history) < PREMIUM_MOMENTUM_MIN_HISTORY:
        return None
    return compute_premium_momentum(premium_history)


def _strength(oi: int, total_oi: int, signal: str) -> int:
    """0-100: OI-share-of-chain (capped per _STRENGTH_SHARE_CAP_PTS) scaled
    by a conviction multiplier from the signal type (_STRENGTH_CONVICTION_MULT
    -- fresh positioning counts more than fading positioning) -- transparent
    arithmetic, not a model, normalized by _STRENGTH_MAX_RAW so 100 is the
    genuine mathematical ceiling, not an approximation of one. (Final review
    pass fix: the previous normalizer was a bare literal that didn't match
    the formula's actual max, so the strongest possible real signal could
    only ever reach ~82, never 100 -- silently narrower than the documented
    0-100 range.)"""
    if total_oi <= 0:
        return 0
    share_pts = min(_STRENGTH_SHARE_CAP_PTS, (oi / total_oi) * 100 * _STRENGTH_SHARE_SCALE)
    conviction_mult = _STRENGTH_CONVICTION_MULT.get(signal, 0.5)
    return round(min(100, share_pts * (1 + conviction_mult) / _STRENGTH_MAX_RAW * 100))


def _probability_from_lean(lean: int) -> int:
    """-1/0/1 -> 35/50/65-ish band; -2..2 (net_lean scale, used for the
    combined "net_lean" field elsewhere) also passes through cleanly.
    Clamped to [10, 90] -- never 0% or 100%, matching
    oi_engine.generate_signal's own confidence-clamping philosophy."""
    return max(10, min(90, 50 + lean * 20))


def _ai_strike_score(*, oi_wall_score: float, ce_buildup: str, pe_buildup: str,
                      ce_gamma_exposure: float, pe_gamma_exposure: float,
                      max_gamma_exposure_in_chain: float, ce_prob_itm: float | None,
                      pe_prob_itm: float | None) -> int:
    """0-100 composite: how much this strike matters RIGHT NOW, combining
    OI concentration, build-up conviction, gamma-hedging exposure, and
    how genuinely "in play" it is (ITM probability near 50% = most
    contested; near 0% or 100% = already decided, less relevant).
    Every component is normalized to 0-100 BEFORE weighting (see
    _SCORE_WEIGHTS) so raw units (contracts vs. percent vs. gamma) never
    let one term dominate by scale alone."""
    conviction_score = max(_CONVICTION_POINTS.get(ce_buildup, 0), _CONVICTION_POINTS.get(pe_buildup, 0))

    gamma_exposure = abs(ce_gamma_exposure) + abs(pe_gamma_exposure)
    gamma_score = (gamma_exposure / max_gamma_exposure_in_chain * 100) if max_gamma_exposure_in_chain > 0 else 0

    itm_probs = [p for p in (ce_prob_itm, pe_prob_itm) if p is not None]
    if itm_probs:
        avg_prob = sum(itm_probs) / len(itm_probs)
        itm_contestedness_score = max(0.0, 100 - abs(avg_prob - 0.5) * 200)
    else:
        itm_contestedness_score = 0.0

    total = (
        oi_wall_score * _SCORE_WEIGHTS["oi_wall"] + conviction_score * _SCORE_WEIGHTS["conviction"]
        + gamma_score * _SCORE_WEIGHTS["gamma_exposure"] + itm_contestedness_score * _SCORE_WEIGHTS["itm_contestedness"]
    )
    return round(max(0, min(100, total)))


def build_table(symbol: str, rows: list, *, underlying: float | None = None,
                 expiry_date: dt.date | None = None) -> list:
    """One StrikeIntelligence per strike in `rows` -- the full per-strike
    table the Option Chain-style view displays. `symbol` is needed (not
    optional) for the per-strike history IV Rank/Premium Momentum read --
    the one real behavior change from this module's first version, where
    everything was computable from `rows` alone."""
    if not rows:
        return []

    rows = market_data.fill_missing_greeks(rows, underlying=underlying, expiry_date=expiry_date)
    support, resistance = oi_walls(rows)
    support_strikes = {r.strike for r in support}
    resistance_strikes = {r.strike for r in resistance}
    max_pain = calc_max_pain(rows)
    total_ce_oi = sum(r.ce_oi for r in rows) or 1
    total_pe_oi = sum(r.pe_oi for r in rows) or 1
    total_chain_oi = total_ce_oi + total_pe_oi

    prepared = []
    for r in rows:
        lean = net_oi_buildup_lean(r.ce_signal, r.pe_signal)
        ce_gamma_exposure = r.ce_oi * r.ce_gamma
        pe_gamma_exposure = r.pe_oi * r.pe_gamma
        prepared.append((r, lean, ce_gamma_exposure, pe_gamma_exposure))
    max_gamma_exposure = max(
        (abs(ce_g) + abs(pe_g) for _r, _lean, ce_g, pe_g in prepared), default=0.0,
    )

    table = []
    for r, lean, ce_gamma_exposure, pe_gamma_exposure in prepared:
        history = data_access.recent_strike_history(symbol, r.strike, limit=20)
        ce_ivs = [h.get("ce_iv") for h in history]
        pe_ivs = [h.get("pe_iv") for h in history]

        oi_wall_score = round((r.ce_oi + r.pe_oi) / total_chain_oi * 100, 1) if total_chain_oi else 0.0
        ce_prob_itm = _prob_itm(underlying, r.strike, r.ce_iv, expiry_date, "CE")
        pe_prob_itm = _prob_itm(underlying, r.strike, r.pe_iv, expiry_date, "PE")

        table.append(StrikeIntelligence(
            strike=r.strike,
            is_oi_wall_support=r.strike in support_strikes,
            is_oi_wall_resistance=r.strike in resistance_strikes,
            oi_wall_score=oi_wall_score,
            is_max_pain=(r.strike == max_pain),
            max_pain_distance=round(abs(r.strike - max_pain), 2) if max_pain else 0.0,
            ce_buildup_type=r.ce_signal, pe_buildup_type=r.pe_signal,
            net_lean=lean["overall"],
            ai_buy_probability_pct=_probability_from_lean(lean["ce_lean"]),
            ai_sell_probability_pct=_probability_from_lean(-lean["pe_lean"]),
            support_strength=_strength(r.pe_oi, total_pe_oi, r.pe_signal),
            resistance_strength=_strength(r.ce_oi, total_ce_oi, r.ce_signal),
            ce_gamma_exposure=round(ce_gamma_exposure, 4), pe_gamma_exposure=round(pe_gamma_exposure, 4),
            ce_delta_exposure=round(r.ce_oi * r.ce_delta, 2), pe_delta_exposure=round(r.pe_oi * r.pe_delta, 2),
            ce_prob_itm=ce_prob_itm, pe_prob_itm=pe_prob_itm,
            ce_iv_rank=_iv_rank(r.ce_iv, ce_ivs), pe_iv_rank=_iv_rank(r.pe_iv, pe_ivs),
            ce_premium_momentum=_premium_momentum(history, "ce_ltp"),
            pe_premium_momentum=_premium_momentum(history, "pe_ltp"),
            expected_move_pts=expected_move(underlying, r.ce_iv if r.ce_iv else r.pe_iv, expiry_date),
            ai_strike_score=_ai_strike_score(
                oi_wall_score=oi_wall_score, ce_buildup=r.ce_signal, pe_buildup=r.pe_signal,
                ce_gamma_exposure=ce_gamma_exposure, pe_gamma_exposure=pe_gamma_exposure,
                max_gamma_exposure_in_chain=max_gamma_exposure, ce_prob_itm=ce_prob_itm, pe_prob_itm=pe_prob_itm,
            ),
            ce_oi=r.ce_oi, pe_oi=r.pe_oi, ce_ltp=r.ce_ltp, pe_ltp=r.pe_ltp,
        ))
    return table
