"""
agents/trading_supervisor/supervision_report.py -- "Publish human-
readable explanations for every decision." Same JSON + human-readable
report shape agents.risk_manager.risk_report already established for
Milestone 6 -- deliberately a parallel, not a shared base class (each
package owns its own report type; retrofitting risk_report.py's
already-tested shape into a shared abstraction was judged not worth the
regression risk for this milestone -- see AI_TRADING_SUPERVISOR.md's
documented scope decisions).
"""
import dataclasses
import datetime as dt
import json


@dataclasses.dataclass
class SupervisionReport:
    subject: str  # candidate name, or "agent_health_sweep" for a run_cycle() report
    decision: str  # "APPROVED" | "REQUIRES_REVIEW" | "REJECTED" -- or "" for a non-decision report
    summary: str
    details: dict
    generated_at: str = dataclasses.field(default_factory=lambda: dt.datetime.now().isoformat())

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, indent=2)

    def human_readable(self) -> str:
        lines = [f"[SUPERVISION] {self.subject}" + (f" -> {self.decision}" if self.decision else ""), self.summary]
        for key, value in self.details.items():
            lines.append(f"  {key}: {value}")
        return "\n".join(lines)


def from_verdict(verdict, *, subject: str) -> SupervisionReport:
    details = {
        "risk_gate_status": verdict.risk_gate_status, "market_state": verdict.market_state,
        "conflicts": verdict.conflicts, "data_health": verdict.data_health,
    }
    return SupervisionReport(subject=subject, decision=verdict.decision, summary=verdict.explanation, details=details)
