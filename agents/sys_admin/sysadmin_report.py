"""
agents/sys_admin/sysadmin_report.py -- "Every autonomous action must
log: reason, confidence, evidence, affected components, recovery
outcome." One shared report shape used by every module in this package
-- same JSON + human-readable pattern agents.risk_manager.risk_report
and agents.trading_supervisor.supervision_report already established.
"""
import dataclasses
import datetime as dt
import json


@dataclasses.dataclass
class SysAdminReport:
    module: str  # "orchestrator" | "infra_monitor" | "deployment_manager" | ...
    action: str  # short verb phrase, e.g. "heartbeat_sweep", "backup_verify"
    reason: str
    confidence: int  # 0-100
    evidence: dict
    affected_components: list
    recovery_outcome: str | None  # None when this report isn't about a recovery attempt
    severity: str  # "info" | "warning" | "critical"
    generated_at: str = dataclasses.field(default_factory=lambda: dt.datetime.now().isoformat())

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str, indent=2)

    def human_readable(self) -> str:
        lines = [
            f"[{self.severity.upper()}] {self.module}.{self.action} (confidence {self.confidence}%)",
            self.reason,
        ]
        if self.affected_components:
            lines.append(f"  affected: {', '.join(self.affected_components)}")
        if self.recovery_outcome:
            lines.append(f"  recovery outcome: {self.recovery_outcome}")
        for key, value in self.evidence.items():
            lines.append(f"  evidence.{key}: {value}")
        return "\n".join(lines)


def build(*, module: str, action: str, reason: str, confidence: int, evidence: dict,
          affected_components: list | None = None, recovery_outcome: str | None = None,
          severity: str = "info") -> SysAdminReport:
    return SysAdminReport(
        module=module, action=action, reason=reason, confidence=max(0, min(100, confidence)),
        evidence=evidence, affected_components=affected_components or [],
        recovery_outcome=recovery_outcome, severity=severity,
    )
