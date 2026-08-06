"""
agents/risk_manager/portfolio_monitor.py -- the Live Portfolio Risk
Monitor. "Real-time exposure, portfolio heat, margin utilization, daily
loss tracking, maximum drawdown tracking, concentration risk,
correlation risk, Greeks exposure, risk alerts through event bus,
automatic emergency recommendations." Reads real (paper-trading)
position/wallet data via agents.risk_manager.data_access -- the ONLY
module here that touches that data; every metric below is a pure
function of a list of Position objects once fetched.

Safety: this module NEVER closes a position, cancels an order, or halts
trading -- there is no such method anywhere in this file. Every
"emergency recommendation" is exactly that: a RECOMMENDATION, written to
the audit trail and (when severe) agents.event_bus, for a human to act
on -- the same propose-only posture every other agent in this framework
holds (agents/base_agent.py has no apply/execute method either).

agents.event_bus gets its first real producer here -- built in
Milestone 1, never actually published to until now (an architecture-
review finding from just before this milestone began).
"""
import dataclasses

from .. import config, event_bus
from . import data_access, risk_engine


@dataclasses.dataclass
class RiskAlert:
    metric: str
    severity: str  # "warning" | "critical" -- matches agents.event_bus.VALID_SEVERITIES
    message: str
    value: float
    limit: float
    recommendation: str


@dataclasses.dataclass
class PortfolioSnapshot:
    user_id: int | None
    capital: float
    exposure: float
    portfolio_heat_pct: float
    margin_utilization_pct: float
    daily_pnl: float
    max_drawdown: float
    concentration: dict  # {symbol: pct_of_total_exposure}
    correlation_flags: list  # [(symbol_a, symbol_b, correlation), ...] above the configured threshold
    greeks_exposure: dict  # {"delta", "gamma", "theta", "vega"} -- None for any Greek nothing was available for
    alerts: list
    summary: str


def _position_risk(p: data_access.Position) -> float:
    """What this position loses if stopped out: |entry - sl| * qty when
    an SL is set. Without one, conservatively the FULL premium (entry *
    qty) is treated as at risk -- a naked long option's max loss with no
    stop is the premium paid, and understating that would be the unsafe
    direction to guess wrong in."""
    if p.sl_price is not None:
        return abs(p.entry_price - p.sl_price) * p.qty
    return abs(p.entry_price) * p.qty


def _position_notional(p: data_access.Position) -> float:
    return abs(p.entry_price) * p.qty


def compute_exposure(positions: list) -> float:
    return round(sum(_position_notional(p) for p in positions), 2)


def compute_portfolio_heat(positions: list, *, capital: float) -> float:
    if not capital:
        return 0.0
    risk = sum(_position_risk(p) for p in positions)
    return round(risk / capital * 100, 2)


def compute_margin_utilization(positions: list, *, wallet_balance: float) -> float:
    """capital_at_risk / (capital_at_risk + wallet_balance) -- wallet_balance
    is already net of every open position's entry debit (see
    data_access.wallet_balance's docstring), so adding it back to
    capital_at_risk reconstructs this user's total capital."""
    capital_at_risk = sum(_position_notional(p) for p in positions)
    total_capital = capital_at_risk + wallet_balance
    if total_capital <= 0:
        return 0.0
    return round(capital_at_risk / total_capital * 100, 2)


def compute_concentration(positions: list) -> dict:
    total = sum(_position_notional(p) for p in positions)
    if total <= 0:
        return {}
    by_symbol: dict = {}
    for p in positions:
        by_symbol[p.symbol] = by_symbol.get(p.symbol, 0.0) + _position_notional(p)
    return {symbol: round(amount / total * 100, 2) for symbol, amount in by_symbol.items()}


def symbol_price_correlation(symbol_a: str, symbol_b: str, *, lookback_days: int = 30):
    """Return-correlation between two symbols' recent daily closes --
    "correlation risk between currently open positions" for positions
    still OPEN (no realized P&L series exists for them yet, unlike
    agents.risk_manager.risk_engine's trade-based correlation for
    promotion candidates). Reuses agents.quant_researcher.data_access's
    OHLCV loader rather than a second candle-loading implementation.
    None if either symbol has no local candle archive, or too little
    overlapping history."""
    from ..quant_researcher import data_access as qr_data_access

    def _daily_returns(symbol):
        candles = qr_data_access.load_candles(symbol)
        if candles is None or candles.empty:
            return {}
        daily_close = candles.set_index("datetime")["close"].resample("1D").last().dropna()
        returns = daily_close.pct_change().dropna().tail(lookback_days)
        return {ts.strftime("%Y-%m-%d"): val for ts, val in returns.items()}

    return risk_engine.pearson_correlation(_daily_returns(symbol_a), _daily_returns(symbol_b))


def compute_correlation_flags(positions: list, *, threshold: float) -> list:
    symbols = sorted({p.symbol for p in positions if p.symbol})
    flags = []
    for i, a in enumerate(symbols):
        for b in symbols[i + 1:]:
            corr = symbol_price_correlation(a, b)
            if corr is not None and abs(corr) > threshold:
                flags.append((a, b, corr))
    return flags


def compute_greeks_exposure(positions: list) -> dict:
    """Sums delta/gamma/theta/vega * qty across every open position with
    a resolvable strike + direction, using only already-logged data
    (agents.risk_manager.data_access.latest_greeks_for_strike -- never a
    live broker call). A Greek stays None in the result if not a single
    position could resolve it (e.g. no strikes data logged yet for any
    held symbol) -- distinct from 0.0, which would claim "no exposure"
    rather than "unknown.\""""
    totals = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    resolved = {"delta": False, "gamma": False, "theta": False, "vega": False}
    for p in positions:
        if p.strike is None or p.direction not in ("CE", "PE"):
            continue
        greeks = data_access.latest_greeks_for_strike(p.symbol, p.strike, p.direction)
        if greeks is None:
            continue
        for greek in totals:
            value = greeks.get(greek)
            if value is not None:
                totals[greek] += value * p.qty
                resolved[greek] = True
    return {greek: (round(totals[greek], 4) if resolved[greek] else None) for greek in totals}


def _build_alerts(*, portfolio_heat_pct, margin_utilization_pct, daily_pnl, capital,
                   max_dd, concentration, correlation_flags) -> list:
    alerts = []

    if portfolio_heat_pct > config.RISK_PORTFOLIO_HEAT_LIMIT_PCT:
        severity = "critical" if portfolio_heat_pct > config.RISK_PORTFOLIO_HEAT_LIMIT_PCT * 1.5 else "warning"
        alerts.append(RiskAlert(
            metric="portfolio_heat", severity=severity,
            message=f"portfolio heat {portfolio_heat_pct}% exceeds the {config.RISK_PORTFOLIO_HEAT_LIMIT_PCT}% limit",
            value=portfolio_heat_pct, limit=config.RISK_PORTFOLIO_HEAT_LIMIT_PCT,
            recommendation="halt new entries until existing risk is reduced (close or tighten stops on "
                            "the largest open positions) before taking on more.",
        ))

    daily_loss_pct = round(-daily_pnl / capital * 100, 2) if capital and daily_pnl < 0 else 0.0
    if daily_loss_pct > config.RISK_DAILY_LOSS_LIMIT_PCT:
        severity = "critical" if daily_loss_pct > config.RISK_DAILY_LOSS_LIMIT_PCT * 1.5 else "warning"
        alerts.append(RiskAlert(
            metric="daily_loss", severity=severity,
            message=f"today's realized loss {daily_loss_pct}% of capital exceeds the "
                    f"{config.RISK_DAILY_LOSS_LIMIT_PCT}% daily limit",
            value=daily_loss_pct, limit=config.RISK_DAILY_LOSS_LIMIT_PCT,
            recommendation="stop trading for the remainder of the session -- the daily loss limit exists "
                            "specifically to prevent one bad day from compounding into a worse one.",
        ))

    max_dd_pct = round(max_dd / capital * 100, 2) if capital else 0.0
    if max_dd_pct > config.RISK_MAX_LIVE_DRAWDOWN_PCT:
        alerts.append(RiskAlert(
            metric="max_drawdown", severity="critical",
            message=f"intraday drawdown {max_dd_pct}% exceeds the {config.RISK_MAX_LIVE_DRAWDOWN_PCT}% limit",
            value=max_dd_pct, limit=config.RISK_MAX_LIVE_DRAWDOWN_PCT,
            recommendation="halt all new entries and review every open position for an immediate exit; "
                            "this drawdown level is a capital-preservation threshold, not a soft warning.",
        ))

    if margin_utilization_pct > config.RISK_MAX_MARGIN_UTILIZATION_PCT:
        alerts.append(RiskAlert(
            metric="margin_utilization", severity="warning",
            message=f"margin utilization {margin_utilization_pct}% exceeds the "
                    f"{config.RISK_MAX_MARGIN_UTILIZATION_PCT}% limit",
            value=margin_utilization_pct, limit=config.RISK_MAX_MARGIN_UTILIZATION_PCT,
            recommendation="reduce position sizing on new entries -- too little capital buffer remains "
                            "for further adverse moves.",
        ))

    for symbol, pct in concentration.items():
        if pct > config.RISK_MAX_CONCENTRATION_PCT:
            alerts.append(RiskAlert(
                metric="concentration", severity="warning",
                message=f"{symbol} is {pct}% of total exposure, above the {config.RISK_MAX_CONCENTRATION_PCT}% limit",
                value=pct, limit=config.RISK_MAX_CONCENTRATION_PCT,
                recommendation=f"avoid new {symbol} entries until other positions diversify this concentration.",
            ))

    for a, b, corr in correlation_flags:
        alerts.append(RiskAlert(
            metric="correlation", severity="warning",
            message=f"{a} and {b} are {corr:+.2f} correlated -- a move against one likely moves against both",
            value=corr, limit=config.RISK_MAX_STRATEGY_CORRELATION,
            recommendation=f"treat {a} and {b} positions as one combined exposure for sizing purposes, "
                            f"not two independent ones.",
        ))

    return alerts


def snapshot(user_id: int | None = None) -> PortfolioSnapshot:
    """The Live Portfolio Risk Monitor's single entry point. Publishes
    every alert to agents.event_bus (severity="critical"/"warning",
    matching event_bus.VALID_SEVERITIES) -- callers wanting persistence
    too should pass the result through risk_report.from_portfolio_snapshot()
    and risk_store.record_snapshot()/record_alert() (see api.py)."""
    positions = data_access.all_open_positions(user_id)
    capital = (
        data_access.wallet_balance(user_id) + sum(_position_notional(p) for p in positions)
        if user_id is not None else config.RISK_ACCOUNT_CAPITAL
    )
    import datetime as dt
    today = dt.date.today().isoformat()

    exposure = compute_exposure(positions)
    heat = compute_portfolio_heat(positions, capital=capital)
    margin_util = (
        compute_margin_utilization(positions, wallet_balance=data_access.wallet_balance(user_id))
        if user_id is not None else round(exposure / capital * 100, 2) if capital else 0.0
    )
    daily_pnl = data_access.daily_realized_pnl(user_id, since_date=today)
    dd = risk_engine.max_drawdown(data_access.closed_trade_points_today(user_id, since_date=today))
    concentration = compute_concentration(positions)
    correlation_flags = compute_correlation_flags(positions, threshold=config.RISK_MAX_STRATEGY_CORRELATION)
    greeks = compute_greeks_exposure(positions)

    alerts = _build_alerts(
        portfolio_heat_pct=heat, margin_utilization_pct=margin_util, daily_pnl=daily_pnl,
        capital=capital, max_dd=dd, concentration=concentration, correlation_flags=correlation_flags,
    )
    for alert in alerts:
        event_bus.publish(
            source_agent="risk_manager", event_type="risk_alert",
            payload=dataclasses.asdict(alert), severity=alert.severity,
        )

    summary = (
        f"{len(positions)} open position(s), heat {heat}%, margin {margin_util}%, "
        f"daily P&L {daily_pnl}, {len(alerts)} alert(s)."
    )
    return PortfolioSnapshot(
        user_id=user_id, capital=capital, exposure=exposure, portfolio_heat_pct=heat,
        margin_utilization_pct=margin_util, daily_pnl=daily_pnl, max_drawdown=dd,
        concentration=concentration, correlation_flags=correlation_flags, greeks_exposure=greeks,
        alerts=alerts, summary=summary,
    )
