"""
agents/sys_admin/admin_agent.py -- SystemAdministrator
(agents.base_agent.BaseAgent). "Build BATI's autonomous operating system
responsible for managing the complete AI ecosystem... It never replaces
their logic. It coordinates, validates, monitors and recovers them."

The third agent in this framework to actually subclass BaseAgent and
register via agents.registry, after Milestone 7's TradingSupervisor --
the pre-Milestone-6 architecture review's Critical "dead foundation
code" finding stays closed, not just fixed once.

run_cycle(): one full operations sweep -- agent orchestration health,
infrastructure, security, maintenance -- turned into Finding()s for
anything abnormal, exactly like TradingSupervisor's own run_cycle().
Self-healing is invoked automatically ONLY for the one safe,
bookkeeping-only recovery self_healing.py itself permits (clearing a
crashed agent's flag); every other detected problem becomes a Finding
plus a recorded SysAdminReport recommendation, never an automatic
action -- see self_healing.py's own docstring for why.

on_event(): reacts to critical risk_alert/supervisor_alert/
supervisor_escalation events by recording them into the sysadmin log
too, so the full cross-agent picture (Risk Manager's alerts, Trading
Supervisor's escalations, and System Administrator's own findings) ends
up in one place for the Operations Dashboard.
"""
from .. import event_bus, memory, registry
from ..base_agent import BaseAgent, Finding
from . import infra_monitor, maintenance, orchestrator, security_audit, self_healing, sysadmin_report, sysadmin_store

_ESCALATION_EVENT_TYPES = ("risk_alert", "supervisor_alert", "supervisor_escalation")


@registry.register_agent("sys_admin")
class SystemAdministrator(BaseAgent):
    name = "sys_admin"

    def __init__(self, audit_log_module=None, *, memory_store=None, source_paths=None,
                 db_path: str = "oi_history.db", repo_dir: str = "."):
        super().__init__(audit_log_module)
        self._memory_store = memory_store
        self._source_paths = source_paths or []
        self._db_path = db_path
        self._repo_dir = repo_dir

    def _store(self):
        return self._memory_store or memory.get_memory_store()

    def run_cycle(self) -> list:
        findings = []
        findings.extend(self._orchestration_findings())
        findings.extend(self._infra_findings())
        findings.extend(self._security_findings())
        findings.extend(self._maintenance_findings())

        for finding in findings:
            if finding.severity == "critical":
                event_bus.publish(
                    source_agent=self.name, event_type="sysadmin_alert",
                    payload={"summary": finding.summary, "evidence": finding.evidence}, severity="critical",
                )
        return findings

    def _orchestration_findings(self) -> list:
        findings = []
        snapshot = orchestrator.registry_snapshot(self._store())
        for agent, state in snapshot.items():
            if state.get("crashed"):
                self_healing.heal_agent_crash(agent)  # the ONE automatic recovery this agent performs
                findings.append(Finding(
                    severity="warning", summary=f"{agent} had crashed -- automatically cleared for retry",
                    evidence={"agent": agent, "crash_reason": state.get("crash_reason")},
                ))
            for dep, health in state.get("dependencies", {}).items():
                if not health["healthy"]:
                    findings.append(Finding(
                        severity="critical", summary=f"{agent} depends on {dep}, which is currently unhealthy",
                        evidence={"agent": agent, "dependency": dep},
                    ))
        return findings

    def _infra_findings(self) -> list:
        snapshot = infra_monitor.snapshot(db_path=self._db_path, check_network=False)
        return [Finding(severity=r.severity, summary=r.reason, evidence=r.evidence) for r in snapshot.reports]

    def _security_findings(self) -> list:
        reports = security_audit.run_audit(
            repo_dir=self._repo_dir, scan_paths=self._source_paths, db_path=self._db_path,
        )
        return [Finding(severity=r.severity, summary=r.reason, evidence=r.evidence) for r in reports]

    def _maintenance_findings(self) -> list:
        reports = maintenance.run_maintenance_sweep(
            source_paths=self._source_paths, repo_dir=self._repo_dir, db_path=self._db_path,
        )
        return [Finding(severity=r.severity, summary=r.reason, evidence=r.evidence) for r in reports]

    def on_event(self, event: dict) -> None:
        if event.get("event_type") not in _ESCALATION_EVENT_TYPES or event.get("severity") != "critical":
            return
        payload = event.get("payload_json") or event.get("payload") or {}
        report = sysadmin_report.build(
            module="admin_agent", action="cross_agent_escalation",
            reason=f"received critical {event.get('event_type')} from {event.get('source_agent')}",
            confidence=80, evidence={"original_event": payload}, severity="critical",
        )
        sysadmin_store.record_report(report)
