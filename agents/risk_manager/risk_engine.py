"""
agents/risk_manager/risk_engine.py -- the Promotion Risk Gate's math.
Every function here is a pure function of (trades, portfolio context,
config) -- no I/O, no memory/DB access (that lives in
risk_intelligence.py/risk_store.py) -- so every check is independently
unit-testable against a hand-built list of trade dicts.

Units: every monetary figure here is in the same "points" unit
agents.quant_researcher already uses throughout (see agents/config.py's
Milestone 6 section for why) -- never a lot-size/currency conversion
this codebase doesn't otherwise have.

Position sizing reuses position_sizing.compute_quantity's own "risk_pct"
formula (imported lazily, matching agents.quant_researcher.data_access's
convention for repo-root modules) rather than a second, possibly-
drifting implementation of the same math.
"""
import dataclasses
import random

from .. import config


@dataclasses.dataclass
class RiskCheckResult:
    name: str
    passed: bool
    value: float | None
    limit: float | None
    detail: str


@dataclasses.dataclass
class RiskAssessment:
    risk_score: int  # 0-100, 100 = safest
    decision: str  # "APPROVED" | "REQUIRES_REVIEW" | "REJECTED"
    checks: list
    var: float
    cvar: float
    drawdown_simulation: dict
    stress_test: dict
    correlations: dict
    explanation: str
    # Populated by risk_intelligence.assess() (never by evaluate_promotion()
    # itself, which has no memory access) -- defaults reflect "no memory
    # context was consulted," not "nothing was found."
    failure_pattern: object = None
    known_bad_configuration: bool = False
    parameter_recommendations: list = dataclasses.field(default_factory=list)


def sector_for(symbol: str) -> str:
    """Falls back to the symbol itself as a one-symbol "sector" for
    anything not in config.RISK_SYMBOL_SECTORS -- under-groups an
    unmapped symbol rather than crashing exposure math."""
    return config.RISK_SYMBOL_SECTORS.get(symbol, symbol)


# --- position sizing / capital allocation / exposure -----------------------

def recommended_quantity(stop_points: float, *, capital: float, risk_pct: float) -> int:
    import position_sizing
    return position_sizing.compute_quantity(
        entry=stop_points, initial_sl=0, sizing_mode="risk_pct",
        capital=capital, risk_pct=risk_pct, min_qty=0,
    )


def position_sizing_check(stop_points: float, *, capital: float, risk_pct: float) -> RiskCheckResult:
    qty = recommended_quantity(stop_points, capital=capital, risk_pct=risk_pct)
    passed = qty >= 1
    detail = (
        f"at {risk_pct}% risk per trade on capital {capital:,.0f}, a {stop_points}-point "
        f"stop sizes to {qty} unit(s)"
        if passed else
        f"a {stop_points}-point stop sizes to 0 units at {risk_pct}% risk per trade on "
        f"capital {capital:,.0f} -- stop too wide for the configured risk budget"
    )
    return RiskCheckResult(name="position_sizing", passed=passed, value=float(qty), limit=1.0, detail=detail)


def capital_allocation_check(candidate_risk_pct: float, active_risk_pct_total: float, *,
                              limit_pct: float) -> RiskCheckResult:
    total = round(candidate_risk_pct + active_risk_pct_total, 4)
    passed = total <= limit_pct
    return RiskCheckResult(
        name="capital_allocation", passed=passed, value=total, limit=limit_pct,
        detail=f"candidate ({candidate_risk_pct}%) + {active_risk_pct_total}% already deployed "
               f"= {total}% of capital vs a {limit_pct}% ceiling",
    )


def exposure_check(name: str, candidate_risk_pct: float, matching_active_risk_pct: float, *,
                    limit_pct: float, group_label: str) -> RiskCheckResult:
    total = round(candidate_risk_pct + matching_active_risk_pct, 4)
    passed = total <= limit_pct
    return RiskCheckResult(
        name=name, passed=passed, value=total, limit=limit_pct,
        detail=f"{group_label}: candidate ({candidate_risk_pct}%) + {matching_active_risk_pct}% "
               f"already deployed there = {total}% vs a {limit_pct}% ceiling",
    )


def concurrent_trades_check(active_count: int, *, limit: int) -> RiskCheckResult:
    total = active_count + 1  # + the candidate itself
    passed = total <= limit
    return RiskCheckResult(
        name="concurrent_trades", passed=passed, value=float(total), limit=float(limit),
        detail=f"{total} concurrently active strategies (including this candidate) vs a limit of {limit}",
    )


# --- correlation -------------------------------------------------------------

def daily_pnl_series(trades: list) -> dict:
    daily: dict = {}
    for t in trades:
        exit_time = t.get("exit_time")
        if exit_time is None:
            continue
        date_key = exit_time.strftime("%Y-%m-%d") if hasattr(exit_time, "strftime") else str(exit_time)[:10]
        daily[date_key] = daily.get(date_key, 0.0) + (t.get("points") or 0.0)
    return daily


def pearson_correlation(series_a: dict, series_b: dict):
    """None (not 0.0) when there isn't enough overlapping history to say
    anything -- absence of evidence is not evidence of zero correlation,
    and compute_risk_score treats None as a small penalty, never a free
    pass."""
    common = sorted(set(series_a) & set(series_b))
    if len(common) < 2:
        return None
    xs = [series_a[d] for d in common]
    ys = [series_b[d] for d in common]
    n = len(xs)
    mean_x, mean_y = sum(xs) / n, sum(ys) / n
    cov = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x == 0 or var_y == 0:
        return None
    return round(cov / ((var_x ** 0.5) * (var_y ** 0.5)), 4)


def correlation_analysis(candidate_trades: list, active_strategies: list) -> dict:
    """active_strategies: [{"strategy_name": ..., "trades": [...]}]. Returns
    {strategy_name: correlation_or_None}."""
    candidate_series = daily_pnl_series(candidate_trades)
    return {
        s["strategy_name"]: pearson_correlation(candidate_series, daily_pnl_series(s.get("trades") or []))
        for s in active_strategies
    }


# --- VaR / CVaR (Expected Shortfall) -----------------------------------------

def value_at_risk(points: list, confidence: float) -> float:
    """Historical-simulation VaR -- a POSITIVE number: the loss such that
    `confidence` fraction of observed outcomes were no worse. 0.0 if
    nothing in the sample lost money at that percentile."""
    if not points:
        return 0.0
    sorted_points = sorted(points)
    n = len(sorted_points)
    # The worst (1-confidence) fraction of the sample occupies indices
    # [0, count); the boundary observation is the LAST of those, i.e.
    # index (count - 1), never count itself (that's already one step
    # inside the "acceptable" 95%).
    idx = max(int((1 - confidence) * n) - 1, 0)
    return round(max(-sorted_points[idx], 0.0), 2)


def expected_shortfall(points: list, confidence: float) -> float:
    """CVaR -- the average loss across the worst (1-confidence) tail,
    always >= VaR at the same confidence level."""
    if not points:
        return 0.0
    sorted_points = sorted(points)
    n = len(sorted_points)
    idx = max(int((1 - confidence) * n), 1)
    tail = sorted_points[:idx]
    return round(max(-(sum(tail) / len(tail)), 0.0), 2)


# --- drawdown simulation / stress testing -------------------------------------

def max_drawdown(points: list) -> float:
    equity = peak = max_dd = 0.0
    for p in points:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def simulate_drawdown_distribution(points: list, *, trials: int, percentile: float,
                                    rng: random.Random | None = None) -> dict:
    """Bootstraps `trials` alternate trade sequences (resampling WITH
    replacement -- the standard bootstrap, stdlib random only) from the
    realized trade points and reports the resulting max-drawdown
    distribution. This is "what could a plausible reordering/repeat of
    this same track record have looked like," not a claim about the
    future -- exactly the same "the backtest sample is representative"
    assumption agents.quant_researcher.statistics_validation already
    leans on."""
    if not points:
        return {"mean": 0.0, "percentile": 0.0, "worst": 0.0, "trials": 0}
    rng = rng or random.Random()
    n = len(points)
    drawdowns = sorted(max_drawdown(rng.choices(points, k=n)) for _ in range(trials))
    idx = min(int((percentile / 100.0) * len(drawdowns)), len(drawdowns) - 1)
    return {
        "mean": round(sum(drawdowns) / len(drawdowns), 2),
        "percentile": round(drawdowns[idx], 2),
        "worst": round(drawdowns[-1], 2),
        "trials": trials,
    }


def stress_test(points: list, shocks: tuple) -> dict:
    """For each shock (a negative fraction, e.g. -0.5 = "losses 50%
    worse"), recomputes net P&L and max drawdown as if every LOSING trade
    in the sample had been that much worse. Winning trades are never
    shocked -- a stress test asks "how bad could the bad days have been,"
    not "what if winners had also lost.\""""
    results = {}
    for shock in shocks:
        shocked = [p * (1 - shock) if p < 0 else p for p in points]
        results[str(shock)] = {"net_pnl": round(sum(shocked), 2), "max_drawdown": round(max_drawdown(shocked), 2)}
    return results


# --- composite risk score / decision -----------------------------------------

def compute_risk_score(checks: list, *, var_pct_of_capital: float, cvar_pct_of_capital: float,
                        drawdown_sim_pct_of_capital: float, worst_stress_pct_of_capital: float,
                        correlation_flags: int) -> int:
    """Starts at 100 (safest) and deducts:
    - 15 points per FAILED hard check (position sizing, capital
      allocation, each exposure limit, concurrent trades) -- these are
      config-defined ceilings, a breach is a breach regardless of degree.
    - up to 20 points scaled by how far VaR-as-%-of-capital exceeds
      RISK_MAX_RISK_PER_TRADE_PCT (VaR at the trade level shouldn't
      routinely exceed what a single trade is allowed to risk).
    - up to 20 points the same way for CVaR (double-weighted relative to
      VaR's scale since CVaR is a tail-average, structurally larger).
    - up to 20 points scaled by how far the 95th-percentile simulated
      drawdown exceeds RISK_MAX_SIMULATED_DRAWDOWN_PCT.
    - up to 15 points scaled by how far the worst stress-tested drawdown
      exceeds RISK_MAX_SIMULATED_DRAWDOWN_PCT (a looser cap than the
      unstressed one -- a stress test is explicitly a worse-than-realized
      scenario).
    - 5 points per strategy correlated above RISK_MAX_STRATEGY_CORRELATION
      (including an unknown/None correlation, since absence of evidence
      isn't evidence of safety).
    Clamped to [0, 100]. Deliberately simple, additive, and fully
    reconstructable from the RiskAssessment's own `checks`/`var`/`cvar`/
    `drawdown_simulation`/`stress_test`/`correlations` fields -- nothing
    about this score is opaque."""
    score = 100
    score -= 15 * sum(1 for c in checks if not c.passed)

    def _scaled_penalty(actual_pct, limit_pct, cap):
        if limit_pct <= 0 or actual_pct <= limit_pct:
            return 0
        overage = (actual_pct - limit_pct) / limit_pct
        return min(cap, round(cap * overage))

    score -= _scaled_penalty(var_pct_of_capital, config.RISK_MAX_RISK_PER_TRADE_PCT, 20)
    score -= _scaled_penalty(cvar_pct_of_capital, config.RISK_MAX_RISK_PER_TRADE_PCT, 20)
    score -= _scaled_penalty(drawdown_sim_pct_of_capital, config.RISK_MAX_SIMULATED_DRAWDOWN_PCT, 20)
    score -= _scaled_penalty(worst_stress_pct_of_capital, config.RISK_MAX_SIMULATED_DRAWDOWN_PCT, 15)
    score -= 5 * correlation_flags

    return max(0, min(100, score))


def decide(risk_score: int) -> str:
    if risk_score < config.RISK_SCORE_REJECT_BELOW:
        return "REJECTED"
    if risk_score < config.RISK_SCORE_REVIEW_BELOW:
        return "REQUIRES_REVIEW"
    return "APPROVED"


def evaluate_promotion(*, candidate_name: str, symbol: str, strategy_family: str,
                        stop_points: float, trades: list, active_strategies: list,
                        capital: float | None = None) -> RiskAssessment:
    """The Promotion Risk Gate's single entry point.

    active_strategies: [{"strategy_name", "symbol", "trades", "risk_pct"}]
    -- everything currently promoted (agents.memory's is_best parameter
    sets), built by risk_intelligence.py so this module never touches
    agents.memory directly (pure function of its arguments, same
    separation agents.quant_researcher.metrics/statistics_validation
    already follow relative to their own orchestrator)."""
    capital = capital if capital is not None else config.RISK_ACCOUNT_CAPITAL
    points = [t.get("points") or 0.0 for t in trades]

    candidate_risk_pct = config.RISK_MAX_RISK_PER_TRADE_PCT
    active_risk_total = sum(s.get("risk_pct", config.RISK_MAX_RISK_PER_TRADE_PCT) for s in active_strategies)
    same_symbol_risk = sum(
        s.get("risk_pct", config.RISK_MAX_RISK_PER_TRADE_PCT)
        for s in active_strategies if s.get("symbol") == symbol
    )
    same_sector_risk = sum(
        s.get("risk_pct", config.RISK_MAX_RISK_PER_TRADE_PCT)
        for s in active_strategies if sector_for(s.get("symbol")) == sector_for(symbol)
    )
    same_family_risk = sum(
        s.get("risk_pct", config.RISK_MAX_RISK_PER_TRADE_PCT)
        for s in active_strategies if s.get("strategy_name") == strategy_family
    )

    checks = [
        position_sizing_check(stop_points, capital=capital, risk_pct=candidate_risk_pct),
        capital_allocation_check(candidate_risk_pct, active_risk_total,
                                  limit_pct=config.RISK_MAX_CAPITAL_ALLOCATION_PCT),
        exposure_check("exposure_symbol", candidate_risk_pct, same_symbol_risk,
                       limit_pct=config.RISK_MAX_EXPOSURE_PER_SYMBOL_PCT, group_label=f"symbol {symbol}"),
        exposure_check("exposure_sector", candidate_risk_pct, same_sector_risk,
                       limit_pct=config.RISK_MAX_EXPOSURE_PER_SECTOR_PCT,
                       group_label=f"sector {sector_for(symbol)}"),
        exposure_check("exposure_strategy", candidate_risk_pct, same_family_risk,
                       limit_pct=config.RISK_MAX_EXPOSURE_PER_STRATEGY_PCT,
                       group_label=f"strategy family {strategy_family}"),
        concurrent_trades_check(len(active_strategies), limit=config.RISK_MAX_CONCURRENT_STRATEGIES),
    ]

    var = value_at_risk(points, config.RISK_VAR_CONFIDENCE)
    cvar = expected_shortfall(points, config.RISK_CVAR_CONFIDENCE)
    drawdown_sim = simulate_drawdown_distribution(
        points, trials=config.RISK_DRAWDOWN_SIMULATION_TRIALS,
        percentile=config.RISK_DRAWDOWN_SIMULATION_PERCENTILE,
    )
    stress = stress_test(points, config.RISK_STRESS_TEST_SHOCKS)
    worst_stress_dd = max((v["max_drawdown"] for v in stress.values()), default=0.0)

    correlations = correlation_analysis(trades, active_strategies)
    correlation_flags = sum(
        1 for c in correlations.values() if c is None or abs(c) > config.RISK_MAX_STRATEGY_CORRELATION
    )

    risk_score = compute_risk_score(
        checks,
        var_pct_of_capital=round(var / capital * 100, 4) if capital else 0.0,
        cvar_pct_of_capital=round(cvar / capital * 100, 4) if capital else 0.0,
        drawdown_sim_pct_of_capital=round(drawdown_sim["percentile"] / capital * 100, 4) if capital else 0.0,
        worst_stress_pct_of_capital=round(worst_stress_dd / capital * 100, 4) if capital else 0.0,
        correlation_flags=correlation_flags,
    )
    decision = decide(risk_score)

    failed_names = [c.name for c in checks if not c.passed]
    explanation_parts = [f"Risk score {risk_score}/100 -> {decision} for {candidate_name} ({symbol})."]
    if failed_names:
        explanation_parts.append(f"Failed hard limits: {', '.join(failed_names)}.")
    explanation_parts.append(
        f"VaR({config.RISK_VAR_CONFIDENCE:.0%})={var}, CVaR({config.RISK_CVAR_CONFIDENCE:.0%})={cvar}, "
        f"simulated {config.RISK_DRAWDOWN_SIMULATION_PERCENTILE:.0f}th-percentile drawdown="
        f"{drawdown_sim['percentile']}, worst stress-tested drawdown={worst_stress_dd}."
    )
    if correlation_flags:
        explanation_parts.append(
            f"{correlation_flags} active strateg{'y is' if correlation_flags == 1 else 'ies are'} "
            f"highly correlated with (or have no comparable history against) this candidate."
        )

    return RiskAssessment(
        risk_score=risk_score, decision=decision, checks=checks, var=var, cvar=cvar,
        drawdown_simulation=drawdown_sim, stress_test=stress, correlations=correlations,
        explanation=" ".join(explanation_parts),
    )
