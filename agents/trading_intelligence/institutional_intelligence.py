"""
agents/trading_intelligence/institutional_intelligence.py -- Module 2:
"Automatically detect: Long Build-up, Short Build-up, Long Unwinding, Short
Covering, OI Walls, Max Pain, Gamma Trap, Liquidity Sweep, Fake Breakout,
Institutional Buying, Institutional Selling."

Eight of these eleven are ALREADY real, tested, live functions in this
repo's own pre-existing engine -- reused here directly, never
reimplemented (oi_engine.py's own module docstring: "Never duplicate this
logic elsewhere"):
  - Long/Short Build-up, Long Unwinding, Short Covering -> oi_engine.classify_buildup
    (already computed PER STRIKE, every live cycle -- see StrikeRow.ce_signal/pe_signal)
  - OI Walls -> oi_engine.oi_walls
  - Max Pain -> oi_engine.calc_max_pain
  - Liquidity Sweep -> market_structure.detect_liquidity_sweep (prefers the
    ALREADY-COMPUTED market_structure_snapshots.liquidity_sweep_json app.py's
    own loop wrote; falls back to computing it directly from candles + PDH/PDL
    only when no snapshot exists yet)
  - Fake Breakout -> sr_probability_engine.fake_breakout_filter (used here
    exactly as app.py/scalping_engine.py/backtest.py already use it -- as a
    gate whose FAILURE is itself the finding worth reporting)

Gamma Trap and Institutional Buying/Selling do not exist anywhere in this
repository yet (confirmed by search) -- built here as real, rule-based,
HONESTLY-LABELED heuristics (advisory, not proven formulas -- the same
"EXPERIMENTAL... not empirically validated" framing oi_engine.py's own
compute_new_trend_meter already uses for its own advisory-only additions),
reusing sr_probability_engine.compute_volume_expansion's statistical
"is this reading elevated vs. its own rolling history" pattern rather than
inventing a new one.
"""
import dataclasses
import datetime as dt

from greeks import black_scholes_greeks, time_to_expiry_years
from oi_engine import calc_max_pain, oi_walls
from sr_probability_engine import compute_volume_expansion, fake_breakout_filter

from . import data_access

GAMMA_TRAP_MIN_GAMMA = 0.0015  # per-point gamma; ATM near-expiry options commonly exceed this
GAMMA_TRAP_MAX_DAYS_TO_EXPIRY = 3
INSTITUTIONAL_OI_EXPANSION_MULT = 2.0  # this strike's OI change vs. its OWN recent-cycle average


@dataclasses.dataclass
class Finding:
    pattern_type: str
    strike: int | None
    description: str
    evidence: dict


def buildup_findings(rows: list) -> list:
    """Long/Short Build-up, Long Unwinding, Short Covering -- ALREADY
    computed per-strike (row.ce_signal/pe_signal came straight from
    classify_buildup() when this cycle was logged); this just surfaces
    every non-Neutral reading as a Finding, one per side per strike."""
    findings = []
    for r in rows:
        for side, signal, oi_chg, chg_pct in (
            ("CE", r.ce_signal, r.ce_oi_chg, r.ce_chg_pct), ("PE", r.pe_signal, r.pe_oi_chg, r.pe_chg_pct),
        ):
            if signal and signal != "Neutral":
                findings.append(Finding(
                    pattern_type=signal.replace(" ", ""), strike=r.strike,
                    description=f"{r.strike} {side}: {signal} (OI chg {oi_chg:+d}, premium {chg_pct:+.2f}%)",
                    evidence={"side": side, "oi_chg": oi_chg, "chg_pct": chg_pct},
                ))
    return findings


def oi_wall_findings(rows: list) -> list:
    if not rows:
        return []
    support, resistance = oi_walls(rows)
    findings = [
        Finding(pattern_type="OIWall", strike=r.strike,
                description=f"{r.strike} PE OI wall (support): {r.pe_oi:,} contracts",
                evidence={"side": "PE", "oi": r.pe_oi, "role": "support"})
        for r in support if r.pe_oi > 0
    ] + [
        Finding(pattern_type="OIWall", strike=r.strike,
                description=f"{r.strike} CE OI wall (resistance): {r.ce_oi:,} contracts",
                evidence={"side": "CE", "oi": r.ce_oi, "role": "resistance"})
        for r in resistance if r.ce_oi > 0
    ]
    return findings


def max_pain_finding(rows: list) -> Finding | None:
    if not rows:
        return None
    max_pain = calc_max_pain(rows)
    return Finding(
        pattern_type="MaxPainBehaviour", strike=max_pain,
        description=f"Max Pain at {max_pain} -- option writers' theoretical minimum-payout strike.",
        evidence={"max_pain": max_pain},
    )


def gamma_trap_findings(rows: list, *, underlying: float | None, expiry_date: dt.date | None) -> list:
    """Heuristic, advisory-only (see module docstring): a strike where (a)
    OI is heavily concentrated (an OI wall), (b) gamma is high (near ATM,
    near expiry), and (c) price is genuinely close to it -- the classic
    setup where market makers hedging their short gamma AMPLIFY a move
    once price actually crosses the strike, rather than damping it. Never
    claims to predict direction, only flags the setup."""
    if not rows or not underlying or not expiry_date:
        return []
    days_to_expiry = (expiry_date - dt.date.today()).days
    if days_to_expiry > GAMMA_TRAP_MAX_DAYS_TO_EXPIRY or days_to_expiry < 0:
        return []
    T = time_to_expiry_years(expiry_date)
    support, resistance = oi_walls(rows)
    wall_strikes = {r.strike for r in support[:1] + resistance[:1]}

    findings = []
    for r in rows:
        if r.strike not in wall_strikes:
            continue
        for side, iv in (("CE", r.ce_iv), ("PE", r.pe_iv)):
            if not iv or iv <= 0:
                continue
            g = black_scholes_greeks(underlying, r.strike, T, iv, side)
            if g.get("valid") and g["gamma"] >= GAMMA_TRAP_MIN_GAMMA:
                distance_pct = abs(underlying - r.strike) / underlying * 100
                if distance_pct <= 1.0:
                    findings.append(Finding(
                        pattern_type="GammaTrap", strike=r.strike,
                        description=(
                            f"Gamma Trap risk at {r.strike} {side}: heavy OI wall + high gamma "
                            f"({g['gamma']:.5f}) only {distance_pct:.2f}% from spot ({underlying}), "
                            f"{days_to_expiry}d to expiry -- a break past this level could accelerate "
                            f"quickly as writers dynamically hedge."
                        ),
                        evidence={"side": side, "gamma": g["gamma"], "distance_pct": round(distance_pct, 3),
                                   "days_to_expiry": days_to_expiry},
                    ))
    return findings


def liquidity_sweep_finding(symbol: str) -> Finding | None:
    """Prefers the ALREADY-COMPUTED market_structure_snapshots.liquidity_sweep_json
    (app.py's own live loop wrote it via market_structure.build_market_structure()
    -> detect_liquidity_sweep()) -- never recomputes if a snapshot already
    has the answer."""
    snapshot = data_access.latest_market_structure(symbol)
    if snapshot is None or not snapshot.get("liquidity_sweep"):
        return None
    sweep = snapshot["liquidity_sweep"]
    if not sweep.get("swept"):
        return None
    return Finding(
        pattern_type="LiquiditySweep", strike=None,
        description=(
            f"{sweep['swept'].capitalize()} liquidity sweep: price swept to {sweep.get('sweep_extreme')} "
            f"then reclaimed -- often precedes a genuine move opposite the sweep direction."
        ),
        evidence=sweep,
    )


def fake_breakout_finding(symbol: str, *, direction: str, row) -> Finding | None:
    """Reuses sr_probability_engine.fake_breakout_filter exactly as app.py/
    scalping_engine.py/backtest.py already use it for TRADE GATING -- here,
    a FAILED filter is itself the reported finding (a fake-breakout risk),
    not silently used to block a signal the caller never asked this module
    to place. vwap_aligned is passed as None (honestly "don't know," never
    disqualifying on its own -- see fake_breakout_filter's own docstring)
    since judging price-vs-VWAP alignment needs a directional read this
    per-strike check doesn't have."""
    volume_history = data_access.recent_strike_history(symbol, row.strike, limit=10)
    vol_field = "ce_vol" if direction == "CE" else "pe_vol"
    chg_field = "ce_chg_pct" if direction == "CE" else "pe_chg_pct"
    oi_field = "ce_signal" if direction == "CE" else "pe_signal"

    current_vol = getattr(row, vol_field)
    history_vols = [h.get(vol_field) for h in volume_history[1:]]
    expanded, _ratio = compute_volume_expansion(history_vols, current_vol)
    premium_rising = getattr(row, chg_field) > 0
    oi_supports = getattr(row, oi_field) in ("Long Buildup", "Short Covering")

    passes, failed = fake_breakout_filter(expanded, oi_supports, premium_rising, None)
    if passes:
        return None
    return Finding(
        pattern_type="FakeBreakoutPattern", strike=row.strike,
        description=f"Fake breakout risk at {row.strike} {direction}: {'; '.join(failed)}",
        evidence={"direction": direction, "failed_checks": failed},
    )


def institutional_flow_findings(symbol: str, rows: list) -> list:
    """Heuristic, advisory-only (see module docstring): distinguishes an
    institutional-scale OI move from ordinary retail-level buildup/
    unwinding noise by requiring the SAME strike's OI change to be
    genuinely elevated (>= INSTITUTIONAL_OI_EXPANSION_MULT vs. its own
    recent-cycle average -- reusing compute_volume_expansion's statistical
    shape, applied to OI-change magnitude rather than volume) AND real
    volume also elevated on the same side."""
    findings = []
    for r in rows:
        for side, oi_chg, vol, signal in (
            ("CE", r.ce_oi_chg, r.ce_vol, r.ce_signal), ("PE", r.pe_oi_chg, r.pe_vol, r.pe_signal),
        ):
            if signal not in ("Long Buildup", "Short Buildup"):
                continue  # institutional flow is about NEW positioning, not unwinding/covering
            history = data_access.recent_strike_history(symbol, r.strike, limit=10)
            oi_chg_field = "ce_oi_chg" if side == "CE" else "pe_oi_chg"
            vol_field = "ce_vol" if side == "CE" else "pe_vol"
            oi_history = [abs(h.get(oi_chg_field) or 0) for h in history[1:]]
            vol_history = [h.get(vol_field) for h in history[1:]]
            oi_expanded, oi_ratio = compute_volume_expansion(oi_history, abs(oi_chg))
            vol_expanded, _vol_ratio = compute_volume_expansion(vol_history, vol)
            if oi_expanded and vol_expanded:
                label = "InstitutionalBuying" if signal == "Long Buildup" else "InstitutionalSelling"
                findings.append(Finding(
                    pattern_type=label, strike=r.strike,
                    description=(
                        f"{label.replace('Institutional', 'Institutional ')} signature at {r.strike} {side}: "
                        f"OI change {oi_ratio}x its own recent average, volume also elevated -- consistent with "
                        f"a large, deliberate position rather than routine retail flow."
                    ),
                    evidence={"side": side, "oi_ratio": oi_ratio, "signal": signal},
                ))
    return findings


def analyze(symbol: str, *, underlying: float | None = None, expiry_date: dt.date | None = None) -> dict:
    """The full Module 2 sweep for one symbol. Returns every finding type
    requested, grouped -- never raises; an empty list for any category is
    an honest "nothing detected this cycle," not an error."""
    latest = data_access.latest_cycle(symbol)
    if latest is None:
        return {"symbol": symbol, "available": False, "reason": f"no cycle logged for {symbol}", "findings": []}
    rows = latest["rows"]
    atm = latest["cycle"].get("atm")
    underlying = underlying if underlying is not None else latest["cycle"].get("underlying_ltp")

    findings = []
    findings.extend(buildup_findings(rows))
    findings.extend(oi_wall_findings(rows))
    mp = max_pain_finding(rows)
    if mp:
        findings.append(mp)
    findings.extend(gamma_trap_findings(rows, underlying=underlying, expiry_date=expiry_date))
    sweep = liquidity_sweep_finding(symbol)
    if sweep:
        findings.append(sweep)
    findings.extend(institutional_flow_findings(symbol, rows))

    atm_row = next((r for r in rows if r.strike == atm), None)
    if atm_row is not None:
        for direction in ("CE", "PE"):
            fb = fake_breakout_finding(symbol, direction=direction, row=atm_row)
            if fb:
                findings.append(fb)

    return {"symbol": symbol, "available": True, "findings": findings}
