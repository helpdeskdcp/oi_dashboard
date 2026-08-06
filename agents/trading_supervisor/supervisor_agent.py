"""
agents/trading_supervisor/supervisor_agent.py -- TradingSupervisor
(agents.base_agent.BaseAgent). "Oversee all other BATI agents and the
live trading workflow."

The first concrete agent in this framework to actually subclass
BaseAgent and register via agents.registry -- every prior milestone
shipped as a free-standing function module instead (agents/dev_agent/
pipeline.py, agents/quant_researcher/research_engine.py), a drift the
pre-Milestone-6 architecture review flagged as a Critical finding. This
is where it stops, not where it deepens: future agents should follow
this class's shape, not the earlier function-module precedent.

run_cycle(): a full sweep -- every agent's recent health
(agent_health.py), every watched symbol's data-feed health
(data_health.py) and volatility state (market_state.py) -- turned into
Finding()s for anything abnormal, persisted to supervision_store.py for
"a complete supervision log for auditing," and (for critical findings)
published to agents.event_bus so other agents/observers can react
without polling. Read-only: this method never calls propose() and never
takes any action beyond recording and alerting -- exactly "trigger
alerts instead of automatic execution when uncertainty is high."

on_event(): reacts to agents.risk_manager.portfolio_monitor's
"risk_alert" events (agents.event_bus's first real producer, Milestone
6) -- a critical one gets escalated to the supervisor's own event-bus
channel. Event shape assumed here matches agents.event_bus.events_since()'s
own return shape ({"source_agent", "event_type", "payload_json",
"severity", ...}) -- the only shape this codebase currently defines for
an "event," since no dispatcher has called on_event() anywhere before
this milestone.
"""
from .. import event_bus, memory, registry
from ..base_agent import BaseAgent, Finding
from . import agent_health, data_health, market_state, supervision_store

DEFAULT_WATCHED_SYMBOLS = ("NIFTY", "BANKNIFTY")


@registry.register_agent("trading_supervisor")
class TradingSupervisor(BaseAgent):
    name = "trading_supervisor"

    def __init__(self, audit_log_module=None, *, memory_store=None, watched_symbols=DEFAULT_WATCHED_SYMBOLS):
        super().__init__(audit_log_module)
        self._memory_store = memory_store
        self._watched_symbols = watched_symbols

    def _store(self):
        # Same optional-injection-else-config-default pattern
        # agents.dev_agent/agents.quant_researcher already use for
        # memory_store -- production callers get agents.memory.
        # get_memory_store(); tests inject a throwaway store.
        return self._memory_store or memory.get_memory_store()

    def run_cycle(self) -> list:
        findings = []

        health = agent_health.sweep_all_agents(self._store())
        for agent_name, h in health.items():
            findings.extend(self._agent_health_findings(agent_name, h))
            self._record_health_snapshot(agent_name, h)

        for symbol in self._watched_symbols:
            findings.extend(self._market_and_feed_findings(symbol))

        for finding in findings:
            if finding.severity == "critical":
                event_bus.publish(
                    source_agent=self.name, event_type="supervisor_alert",
                    payload={"summary": finding.summary, "evidence": finding.evidence}, severity="critical",
                )
        return findings

    def _record_health_snapshot(self, agent_name, health) -> None:
        if agent_name == "memory":
            supervision_store.record_agent_health(
                "memory", is_stale=health.is_stale, is_failing=not health.reachable,
                snapshot={"reachable": health.reachable, "most_recent_write_ts": health.most_recent_write_ts},
            )
            return
        supervision_store.record_agent_health(
            agent_name, is_stale=health.is_stale, is_failing=health.is_failing,
            snapshot={
                "recent_activity_count": health.recent_activity_count,
                "outcome_counts": health.outcome_counts, "last_activity_ts": health.last_activity_ts,
            },
        )

    def _agent_health_findings(self, agent_name, health) -> list:
        if agent_name == "memory":
            if not health.reachable:
                return [Finding(
                    severity="critical", summary="Memory store is unreachable",
                    evidence={"agent": "memory", "reachable": False},
                )]
            if health.is_stale:
                return [Finding(
                    severity="warning", summary="Memory has not been written to recently",
                    evidence={"agent": "memory", "most_recent_write_ts": health.most_recent_write_ts},
                )]
            return []

        if health.is_failing:
            return [Finding(
                severity="critical", summary=f"{agent_name} has an abnormally high recent failure rate",
                evidence={
                    "agent": agent_name, "outcome_counts": health.outcome_counts,
                    "recent_activity_count": health.recent_activity_count,
                },
            )]
        return []

    def _market_and_feed_findings(self, symbol: str) -> list:
        findings = []
        feed = data_health.check_feed_staleness(symbol)
        if feed.is_stale:
            findings.append(Finding(
                severity="warning", summary=f"data feed for {symbol} looks stale",
                evidence={"symbol": symbol, "note": feed.note, "staleness_minutes": feed.staleness_minutes},
            ))
        volatility = market_state.volatility_regime()
        if volatility.get("level") == "high":
            findings.append(Finding(
                severity="warning",
                summary=f"elevated volatility (INDIA VIX) -- affects {symbol} and every other symbol",
                evidence={"symbol": symbol, "vix": volatility.get("vix"), "percentile": volatility.get("percentile")},
            ))
        return findings

    def on_event(self, event: dict) -> None:
        if event.get("event_type") != "risk_alert" or event.get("severity") != "critical":
            return
        payload = event.get("payload_json") or event.get("payload") or {}
        event_bus.publish(
            source_agent=self.name, event_type="supervisor_escalation",
            payload={"original_event": payload}, severity="critical",
        )
