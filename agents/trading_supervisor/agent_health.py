"""
agents/trading_supervisor/agent_health.py -- "Monitor all AI agents
(Developer, Memory, Quant Researcher, Risk Manager)." Three of those
record their own audit trail (dev_agent, quant_researcher, risk_manager
-- the literal `agent` column values agents.audit_log.record already
uses); Memory has no audit trail of its own (a passive store other
agents write into), so its health is checked directly instead.
"""
import dataclasses
import datetime as dt

from .. import audit_log

AGENT_NAMES = ("dev_agent", "quant_researcher", "risk_manager")


@dataclasses.dataclass
class AgentHealth:
    agent: str
    recent_activity_count: int
    outcome_counts: dict
    last_activity_ts: str | None
    is_stale: bool
    is_failing: bool


@dataclasses.dataclass
class MemoryHealth:
    reachable: bool
    most_recent_write_ts: str | None
    is_stale: bool


def _outcome_counts(rows: list) -> dict:
    counts: dict = {}
    for r in rows:
        outcome = r.get("outcome") or "unknown"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts


def agent_activity_health(agent: str, *, since_hours: int = 24, limit: int = 100,
                           staleness_hours: int = 48, failure_rate_threshold: float = 0.7) -> AgentHealth:
    """is_stale: no activity at all within staleness_hours -- could mean
    "nothing to do" (benign) or "this agent stopped running" (not); left
    for supervision_engine.py to weigh alongside everything else, not
    treated as a hard failure here. is_failing: recent activity is
    disproportionately rejected/failed -- a real behavioural signal, not
    a staleness one."""
    since_ts = (dt.datetime.now() - dt.timedelta(hours=since_hours)).isoformat()
    rows = audit_log.list_recent(agent=agent, since_ts=since_ts, limit=limit)
    counts = _outcome_counts(rows)
    last_ts = rows[0]["ts"] if rows else None

    is_stale = True
    if last_ts:
        try:
            age_hours = (dt.datetime.now() - dt.datetime.fromisoformat(last_ts)).total_seconds() / 3600
            is_stale = age_hours > staleness_hours
        except ValueError:
            is_stale = True

    total = len(rows)
    failures = counts.get("rejected", 0) + counts.get("failed", 0)
    is_failing = total > 0 and (failures / total) >= failure_rate_threshold

    return AgentHealth(
        agent=agent, recent_activity_count=total, outcome_counts=counts,
        last_activity_ts=last_ts, is_stale=is_stale, is_failing=is_failing,
    )


def memory_health(memory_store, *, staleness_hours: int = 168) -> MemoryHealth:
    """"Reachable" means every read below completed without raising --
    a real connectivity/schema check, not a ping. Freshness uses the
    most recent row across whichever categories have any rows at all --
    Memory can legitimately be "healthy but quiet" for a category
    nothing has written to yet, which is never treated as staleness."""
    try:
        candidates = []
        for hits in (
            memory_store.search_bug_fixes("", limit=1),
            memory_store.list_backtest_history(limit=1),
            memory_store.search_failed_experiments("", limit=1),
        ):
            if hits:
                candidates.append(hits[0].get("ts"))
    except Exception:
        return MemoryHealth(reachable=False, most_recent_write_ts=None, is_stale=True)

    most_recent = max((c for c in candidates if c), default=None)
    if most_recent is None:
        return MemoryHealth(reachable=True, most_recent_write_ts=None, is_stale=False)
    try:
        age_hours = (dt.datetime.now() - dt.datetime.fromisoformat(most_recent)).total_seconds() / 3600
        is_stale = age_hours > staleness_hours
    except ValueError:
        is_stale = True
    return MemoryHealth(reachable=True, most_recent_write_ts=most_recent, is_stale=is_stale)


def sweep_all_agents(memory_store, **kwargs) -> dict:
    result = {agent: agent_activity_health(agent, **kwargs) for agent in AGENT_NAMES}
    result["memory"] = memory_health(memory_store)
    return result
