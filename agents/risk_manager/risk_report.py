"""
agents/risk_manager/risk_report.py -- "Every risk decision logged.
Human-readable explanation. JSON risk report." One shared report shape
for both the Promotion Risk Gate (risk_engine.RiskAssessment /
risk_intelligence.assess()) and the Live Portfolio Risk Monitor
(portfolio_monitor.py's snapshots/alerts), so a human reading
risk_assessments and risk_alerts side by side sees the same structure
regardless of which subsystem produced it.
"""
import dataclasses
import datetime as dt
import json


@dataclasses.dataclass
class RiskReport:
    report_type: str  # "promotion" | "portfolio_snapshot" | "alert"
    subject: str  # strategy name for a promotion, "portfolio" (or a user id) otherwise
    risk_score: int | None  # 0-100, only ever set for report_type="promotion"
    decision: str | None  # APPROVED/REQUIRES_REVIEW/REJECTED, only for "promotion"
    summary: str  # one-line human-readable explanation
    details: dict  # full structured payload -- everything the JSON report needs
    generated_at: str = dataclasses.field(default_factory=lambda: dt.datetime.now().isoformat())

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, indent=2)

    def human_readable(self) -> str:
        header = f"[{self.report_type.upper()}] {self.subject}"
        lines = [f"{header} -- {self.summary}"]
        if self.risk_score is not None:
            tail = f" -> {self.decision}" if self.decision else ""
            lines.append(f"Risk score: {self.risk_score}/100{tail}")
        for key, value in self.details.items():
            lines.append(f"  {key}: {value}")
        return "\n".join(lines)


def from_risk_assessment(assessment, *, subject: str) -> RiskReport:
    """Wraps a risk_engine.RiskAssessment / risk_intelligence.assess()
    result -- the Promotion Risk Gate's output."""
    checks = [
        {"name": c.name, "passed": c.passed, "value": c.value, "limit": c.limit, "detail": c.detail}
        for c in assessment.checks
    ]
    details = {
        "checks": checks,
        "var": assessment.var, "cvar": assessment.cvar,
        "drawdown_simulation": assessment.drawdown_simulation,
        "stress_test": assessment.stress_test,
        "correlations": assessment.correlations,
        "known_bad_configuration": assessment.known_bad_configuration,
        "parameter_recommendations": [dataclasses.asdict(r) for r in assessment.parameter_recommendations],
    }
    if assessment.failure_pattern is not None:
        details["failure_pattern"] = dataclasses.asdict(assessment.failure_pattern)
    return RiskReport(
        report_type="promotion", subject=subject, risk_score=assessment.risk_score,
        decision=assessment.decision, summary=assessment.explanation, details=details,
    )


def from_portfolio_snapshot(snapshot: dict, *, subject: str = "portfolio") -> RiskReport:
    """Wraps a portfolio_monitor.PortfolioSnapshot (as a dict) -- a
    measurement, not a decision, so risk_score/decision stay None."""
    return RiskReport(
        report_type="portfolio_snapshot", subject=subject, risk_score=None, decision=None,
        summary=snapshot.get("summary", ""), details=snapshot,
    )


def from_alert(alert: dict) -> RiskReport:
    """Wraps one portfolio_monitor.RiskAlert (as a dict)."""
    return RiskReport(
        report_type="alert", subject=alert.get("metric", "unknown"), risk_score=None, decision=None,
        summary=alert.get("message", ""), details=alert,
    )


def unavailable(subject: str, reason: str) -> RiskReport:
    """A live snapshot the underlying data couldn't be read for --
    e.g. oi_history.db missing/locked/corrupted (found during the
    Production Hardening Sprint's DB-failure fault injection: portfolio_monitor.snapshot()
    previously let sqlite3.Error propagate unhandled straight through
    the /api/risk/portfolio Flask route). Honest "unavailable", same
    posture as market_state/data_health's "unknown" -- never a
    fabricated zeroed-out snapshot, and never persisted (there's
    nothing real to log)."""
    return RiskReport(
        report_type="portfolio_snapshot", subject=subject, risk_score=None, decision=None,
        summary="portfolio data unavailable -- database read failed",
        details={"available": False, "reason": reason},
    )
