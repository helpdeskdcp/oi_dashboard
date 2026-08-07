"""
agents/risk_manager/api.py -- the Risk API: plain, JSON-serializable
read functions any caller (a Flask route, a CLI, a notebook) can call
without importing SQLite directly.

get_portfolio_snapshot() is the one function that also WRITES (via
risk_store.py) -- computing a live snapshot and then throwing it away
would violate "every risk decision logged"; calling this from a
dashboard widget is itself a risk decision worth an audit trail, not
just a promotion outcome.
"""
import dataclasses
import sqlite3

from . import portfolio_monitor, risk_report, risk_store


def _snapshot_details(snapshot: portfolio_monitor.PortfolioSnapshot) -> dict:
    return {
        "capital": snapshot.capital, "exposure": snapshot.exposure,
        "portfolio_heat_pct": snapshot.portfolio_heat_pct,
        "margin_utilization_pct": snapshot.margin_utilization_pct,
        "daily_pnl": snapshot.daily_pnl, "max_drawdown": snapshot.max_drawdown,
        "concentration": snapshot.concentration, "correlation_flags": snapshot.correlation_flags,
        "greeks_exposure": snapshot.greeks_exposure,
        "alerts": [dataclasses.asdict(a) for a in snapshot.alerts],
        "summary": snapshot.summary,
    }


def get_portfolio_snapshot(user_id: int | None = None, *, persist: bool = True) -> dict:
    """Computes a fresh live snapshot (agents.risk_manager.portfolio_monitor.snapshot,
    which already publishes any alerts to agents.event_bus) and, unless
    persist=False (tests, dry runs), records it plus every alert to
    risk_store.py so it shows up in get_recent_snapshots()/
    get_recent_alerts() afterward. Returns a plain JSON-serializable
    dict -- safe to hand straight to Flask's jsonify()."""
    subject = f"user:{user_id}" if user_id is not None else "portfolio"
    try:
        snapshot = portfolio_monitor.snapshot(user_id)
    except sqlite3.Error as exc:
        return risk_report.unavailable(subject, str(exc)).to_dict()
    report = risk_report.from_portfolio_snapshot(_snapshot_details(snapshot), subject=subject)

    if persist:
        risk_store.record_snapshot(
            report, user_id=user_id, exposure=snapshot.exposure, portfolio_heat=snapshot.portfolio_heat_pct,
            margin_utilization=snapshot.margin_utilization_pct, daily_pnl=snapshot.daily_pnl,
            max_drawdown=snapshot.max_drawdown,
        )
        for alert in snapshot.alerts:
            alert_report = risk_report.from_alert(dataclasses.asdict(alert))
            risk_store.record_alert(
                alert_report, metric=alert.metric, severity=alert.severity, value=alert.value,
                limit_value=alert.limit, user_id=user_id, recommendation=alert.recommendation,
            )

    return report.to_dict()


def get_recent_assessments(*, symbol: str | None = None, decision: str | None = None, limit: int = 20) -> list:
    """Promotion Risk Gate history -- agents.quant_researcher.research_engine
    candidates the risk gate has evaluated."""
    return risk_store.list_assessments(symbol=symbol, decision=decision, limit=limit)


def get_recent_alerts(*, severity: str | None = None, user_id: int | None = None, limit: int = 20) -> list:
    return risk_store.list_alerts(severity=severity, user_id=user_id, limit=limit)


def get_recent_snapshots(*, user_id: int | None = None, limit: int = 20) -> list:
    return risk_store.list_snapshots(user_id=user_id, limit=limit)
