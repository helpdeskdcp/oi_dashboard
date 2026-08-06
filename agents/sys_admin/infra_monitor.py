"""
agents/sys_admin/infra_monitor.py -- "Monitor: CPU, RAM, Disk, Network,
GPU, SQLite, API latency, Queue length, Thread health."

Stdlib-only -- psutil is not a dependency of this repo (confirmed absent
at Milestone 8's start) and none is added just for this. CPU/RAM read
Linux's own /proc (this deployment's actual platform); both degrade to
"unknown" rather than fabricating a number on a platform where /proc
doesn't exist. "Network" is a real TCP-connect reachability + latency
probe, not fabricated bandwidth/throughput figures psutil's real
interface counters would be needed for. "API latency" measures THIS
repo's own SQLite round-trip time -- never a live Angel One call, the
same landmine agents.risk_manager.data_access and agents.
trading_supervisor.data_health already documented and avoided.
"Queue length" is the real approval backlog (agent_audit_log rows with
outcome='pending_approval') -- the actual queue this codebase has (no
message broker exists, by design; see AUTONOMOUS_AGENTS_ARCHITECTURE.md
principle 3).
"""
import dataclasses
import os
import shutil
import socket
import sqlite3
import threading
import time

from .. import audit_log, config
from . import sysadmin_report, sysadmin_store


@dataclasses.dataclass
class InfraSnapshot:
    cpu: dict
    memory: dict
    disk: dict
    network: dict
    gpu: dict
    sqlite: dict
    queue_length: int
    thread_count: int
    reports: list


def cpu_status() -> dict:
    cores = os.cpu_count()
    try:
        load1, load5, load15 = os.getloadavg()
    except (OSError, AttributeError):
        return {"load1": None, "load5": None, "load15": None, "cores": cores, "load1_per_core": None}
    ratio = round(load1 / cores, 2) if cores else None
    return {"load1": round(load1, 2), "load5": round(load5, 2), "load15": round(load15, 2),
            "cores": cores, "load1_per_core": ratio}


def memory_status() -> dict:
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                info[key] = int(rest.strip().split()[0])  # kB
        total = info.get("MemTotal", 0)
        available = info.get("MemAvailable", info.get("MemFree", 0))
        used_pct = round((total - available) / total * 100, 1) if total else None
        return {"total_kb": total, "available_kb": available, "used_pct": used_pct}
    except (OSError, KeyError, ValueError):
        return {"total_kb": None, "available_kb": None, "used_pct": None}


def disk_status(path: str = ".") -> dict:
    usage = shutil.disk_usage(path)
    used_pct = round(usage.used / usage.total * 100, 1) if usage.total else None
    return {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free, "used_pct": used_pct}


def gpu_status() -> dict:
    """Real, not a placeholder: this deployment has no GPU workload
    anywhere in the codebase, and `nvidia-smi` absence is exactly what
    "no GPU" looks like on Linux -- reported honestly instead of
    fabricating utilization numbers for hardware that isn't there."""
    if shutil.which("nvidia-smi") is None:
        return {"available": False, "note": "no nvidia-smi on PATH -- no GPU detected"}
    return {"available": True, "note": "nvidia-smi present -- GPU-specific metrics not implemented"}


def network_status(hosts=(("8.8.8.8", 53), ("1.1.1.1", 53)), *, timeout: float = 2.0) -> dict:
    """A real TCP-connect reachability + latency probe -- deliberately
    NOT fabricated bandwidth/throughput numbers, which would need
    psutil's real interface counters to measure honestly."""
    results = {}
    for host, port in hosts:
        start = time.monotonic()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                results[host] = {"reachable": True, "latency_ms": round((time.monotonic() - start) * 1000, 1)}
        except OSError as exc:
            results[host] = {"reachable": False, "latency_ms": None, "error": str(exc)}
    return results


def sqlite_status(db_path: str = "oi_history.db") -> dict:
    if not os.path.exists(db_path):
        return {"exists": False, "size_bytes": None, "integrity_ok": None, "query_latency_ms": None}
    size_bytes = os.path.getsize(db_path)
    start = time.monotonic()
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        row = conn.execute("PRAGMA quick_check").fetchone()
        integrity_ok = row is not None and row[0] == "ok"
        conn.close()
    except sqlite3.Error:
        integrity_ok = False
    latency_ms = round((time.monotonic() - start) * 1000, 1)
    return {"exists": True, "size_bytes": size_bytes, "integrity_ok": integrity_ok, "query_latency_ms": latency_ms}


def queue_length() -> int:
    return len(audit_log.list_pending())


def thread_health() -> dict:
    """Threads in the CURRENT process -- if invoked from a different
    process than the live app.py, this reports ITS OWN thread state,
    not the live app's. A documented limitation, not a silent
    inaccuracy."""
    threads = threading.enumerate()
    return {
        "count": len(threads), "names": [t.name for t in threads],
        "alive": sum(1 for t in threads if t.is_alive()),
    }


def _findings(cpu, memory, disk, sqlite_info, queue_len) -> list:
    findings = []

    if cpu.get("load1_per_core") is not None and cpu["load1_per_core"] > config.SYS_ADMIN_CPU_LOAD_WARN_MULTIPLIER:
        findings.append(sysadmin_report.build(
            module="infra_monitor", action="cpu_check",
            reason=f"1-minute load average is {cpu['load1_per_core']}x per-core "
                   f"(warn > {config.SYS_ADMIN_CPU_LOAD_WARN_MULTIPLIER}x)",
            confidence=80, evidence=cpu, affected_components=["host"], severity="warning",
        ))

    used_pct = memory.get("used_pct")
    if used_pct is not None:
        if used_pct >= config.SYS_ADMIN_MEMORY_CRITICAL_PCT:
            findings.append(sysadmin_report.build(
                module="infra_monitor", action="memory_check",
                reason=f"memory usage {used_pct}% >= critical threshold {config.SYS_ADMIN_MEMORY_CRITICAL_PCT}%",
                confidence=90, evidence=memory, affected_components=["host"], severity="critical",
            ))
        elif used_pct >= config.SYS_ADMIN_MEMORY_WARN_PCT:
            findings.append(sysadmin_report.build(
                module="infra_monitor", action="memory_check",
                reason=f"memory usage {used_pct}% >= warn threshold {config.SYS_ADMIN_MEMORY_WARN_PCT}%",
                confidence=80, evidence=memory, affected_components=["host"], severity="warning",
            ))

    disk_pct = disk.get("used_pct")
    if disk_pct is not None:
        if disk_pct >= config.SYS_ADMIN_DISK_CRITICAL_PCT:
            findings.append(sysadmin_report.build(
                module="infra_monitor", action="disk_check",
                reason=f"disk usage {disk_pct}% >= critical threshold {config.SYS_ADMIN_DISK_CRITICAL_PCT}%",
                confidence=90, evidence=disk, affected_components=["host"], severity="critical",
            ))
        elif disk_pct >= config.SYS_ADMIN_DISK_WARN_PCT:
            findings.append(sysadmin_report.build(
                module="infra_monitor", action="disk_check",
                reason=f"disk usage {disk_pct}% >= warn threshold {config.SYS_ADMIN_DISK_WARN_PCT}%",
                confidence=80, evidence=disk, affected_components=["host"], severity="warning",
            ))

    if sqlite_info.get("integrity_ok") is False:
        findings.append(sysadmin_report.build(
            module="infra_monitor", action="sqlite_check", reason="SQLite integrity check failed",
            confidence=95, evidence=sqlite_info, affected_components=["oi_history.db"], severity="critical",
        ))
    latency_ms = sqlite_info.get("query_latency_ms")
    if latency_ms is not None and latency_ms > config.SYS_ADMIN_DB_LATENCY_WARN_MS:
        findings.append(sysadmin_report.build(
            module="infra_monitor", action="sqlite_latency_check",
            reason=f"SQLite round-trip latency {latency_ms}ms > {config.SYS_ADMIN_DB_LATENCY_WARN_MS}ms",
            confidence=70, evidence=sqlite_info, affected_components=["oi_history.db"], severity="warning",
        ))

    if queue_len > config.SYS_ADMIN_QUEUE_LENGTH_WARN:
        findings.append(sysadmin_report.build(
            module="infra_monitor", action="queue_length_check",
            reason=f"{queue_len} proposals pending approval, above the warn threshold of "
                   f"{config.SYS_ADMIN_QUEUE_LENGTH_WARN}",
            confidence=90, evidence={"queue_length": queue_len}, affected_components=["approval_queue"],
            severity="warning",
        ))
    return findings


def snapshot(*, db_path: str = "oi_history.db", disk_path: str = ".", check_network: bool = True) -> InfraSnapshot:
    cpu = cpu_status()
    memory = memory_status()
    disk = disk_status(disk_path)
    network = network_status() if check_network else {}
    gpu = gpu_status()
    sqlite_info = sqlite_status(db_path)
    queue_len = queue_length()
    threads = thread_health()

    findings = _findings(cpu, memory, disk, sqlite_info, queue_len)
    for report in findings:
        sysadmin_store.record_report(report)
    return InfraSnapshot(
        cpu=cpu, memory=memory, disk=disk, network=network, gpu=gpu, sqlite=sqlite_info,
        queue_length=queue_len, thread_count=threads["count"], reports=findings,
    )
