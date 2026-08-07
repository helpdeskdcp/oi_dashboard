"""
agents/sys_admin/api.py -- Operations Dashboard support: plain,
JSON-serializable read functions any caller (a Flask route, a CLI, a
notebook) can call without importing SQLite or the individual sys_admin
modules directly.

"Display: Agent health, Infrastructure, Risk state, Deployment state,
Backup state, Security alerts, Recovery history." get_overview() pulls
all of that into one call; the rest are focused reads for a specific
widget.
"""
import sqlite3

from .. import memory
from ..risk_manager import risk_store
from ..trading_supervisor import supervision_store
from . import infra_monitor, orchestrator, sysadmin_store


def _section(build):
    """Runs one Operations Dashboard section in isolation -- found during
    the Production Hardening Sprint's DB-failure fault injection:
    get_overview() previously ran every section as one unbroken chain, so
    a single missing/corrupted table (e.g. oi_history.db not yet
    initialized) took down the ENTIRE dashboard, including the sections
    that were still perfectly readable. The one view meant to show
    system health during an incident must not itself be the thing that
    goes blank. Each section now degrades independently and honestly
    ({"error": ...}), same posture as market_state/data_health's
    "unknown" elsewhere in this framework -- never fabricated data."""
    try:
        return build()
    except sqlite3.Error as exc:
        return {"error": str(exc)}


def get_overview(*, memory_store=None, db_path: str = "oi_history.db") -> dict:
    """One call, the full Operations Dashboard picture."""
    store = memory_store or memory.get_memory_store()
    return {
        "agents": _section(lambda: orchestrator.registry_snapshot(store)),
        "infrastructure": _section(lambda: _infra_summary(db_path)),
        "risk_state": _section(lambda: {
            "recent_assessments": risk_store.list_assessments(limit=10),
            "recent_alerts": risk_store.list_alerts(limit=10),
        }),
        "supervision_state": _section(lambda: {
            "recent_decisions": supervision_store.list_supervision_log(limit=10),
            "agent_health": supervision_store.list_agent_health(limit=10),
        }),
        "backup_state": _section(lambda: {
            "recent_backups": sysadmin_store.list_backups(limit=10),
            "healthy_backups": sysadmin_store.list_backups(verified_only=True, limit=5),
        }),
        "security_alerts": _section(lambda: sysadmin_store.list_reports(module="security_audit", limit=10)),
        "recovery_history": _section(lambda: sysadmin_store.list_reports(module="self_healing", limit=10)),
        "recent_findings": _section(lambda: sysadmin_store.list_reports(limit=20)),
    }


def _infra_summary(db_path: str) -> dict:
    snapshot = infra_monitor.snapshot(db_path=db_path, check_network=False)
    return {
        "cpu": snapshot.cpu, "memory": snapshot.memory, "disk": snapshot.disk, "gpu": snapshot.gpu,
        "sqlite": snapshot.sqlite, "queue_length": snapshot.queue_length, "thread_count": snapshot.thread_count,
    }


def get_agent_status() -> list:
    return sysadmin_store.list_agent_status()


def get_recent_findings(*, module: str | None = None, severity: str | None = None, limit: int = 20) -> list:
    return sysadmin_store.list_reports(module=module, severity=severity, limit=limit)


def get_backups(*, verified_only: bool = False, limit: int = 20) -> list:
    return sysadmin_store.list_backups(verified_only=verified_only, limit=limit)
