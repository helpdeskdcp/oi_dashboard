"""
agents/runtime/agent_runtime.py -- "Automatically execute: AI Developer,
AI Memory, AI Quant Researcher, AI Risk Manager, AI Trading Supervisor,
AI System Administrator. Each agent must have: heartbeat, status, last
execution, execution duration, failure counter, health score."

THE central module of Milestone 9: this is what actually calls each of
the six already-built agents, closing the #1 finding of
AUTONOMOUS_READINESS_REPORT.md ("no dispatcher ever calls any agent's
run_cycle()"). It changes NOTHING about how any of the six agents make
their own decisions -- it only invokes their existing, already-tested
entrypoints (TradingSupervisor.run_cycle(), SystemAdministrator.
run_cycle(), research_engine.run_research_cycle(),
risk_api.get_portfolio_snapshot(), agent_health.memory_health()) and
records execution bookkeeping via agents.sys_admin.sysadmin_store's
Milestone-9-extended agent_status table (heartbeat/currently_running/
last_execution_ts/last_execution_duration_ms/failure_counter/
health_score) -- reusing that store rather than a second one.

Two of six (dev_agent, quant_researcher) don't have a zero-argument
"just run and find work" entrypoint -- by design, from Milestone 1
onward (dev_agent.detector.detect() evaluates a GIVEN trigger; it was
never built to scan logs for one itself, see
AUTONOMOUS_AGENTS_ARCHITECTURE.md's own AGT-02 description). Rather than
fabricate a fake trigger source, dev_agent's runtime cycle drains real
triggers from agents.runtime.task_queue (task_type="dev_agent_trigger",
enqueued by a human or a future log-tailing integration) and is an
honest no-op when the queue is empty -- giving the Task Queue module a
real purpose rather than existing in isolation from the agents it
should be able to trigger.

A SEVENTH agent, trading_intelligence (Milestone 10), was added to this
dispatcher during M10's final review pass -- closing that milestone's
own "nothing in this milestone is wired into the runtime yet" limitation
the same way this whole module closed AUTONOMOUS_READINESS_REPORT.md's
finding for the original six. Same contract, same everything: it only
invokes agents.trading_intelligence.api.run_scheduled_cycle() (an
existing, already-tested entrypoint), records the same execution
bookkeeping, and is market-hours gated exactly like trading_supervisor
(see agents.runtime.scheduler._MARKET_SESSION_GATED_AGENTS). It is
deliberately NOT added to agents.sys_admin.orchestrator.AGENT_NAMES
(Milestone 8) -- that module is explicitly scoped to four Milestone
2-7 agents and touching it would mean editing a previous milestone's
file for a M10-only concern; trading_intelligence is instead always
considered enabled here, the same way "memory" and "sys_admin"
already are (see run_agent_cycle()'s own orchestrator_agent check
below) -- a bad cycle still fails safely and gets recorded/escalated
by this module's own existing failure-counter logic, uniformly, no
extra gate needed.

An EIGHTH agent, shadow_mode (Milestone 12, Phase 3), was registered
for STATUS/HEALTH TRACKING ONLY -- so it participates in
health_snapshot(), /api/runtime/status's "control" section, and the
sysadmin dashboard's per-agent table the same way every other runtime
agent does. Unlike every other entry here, its cycle function
(_shadow_mode_cycle() below) deliberately never executes Shadow Mode's
actual observation/evaluation logic -- it only reads
agents.shadow_mode.api.get_status() (already a pure, read-only
function) and reports a health finding. shadow_mode_cli.py remains the
ONLY caller of agents.shadow_mode.observer.observe_and_predict()/
evaluator.evaluate_pending() anywhere in this codebase (see that
module's own docstring and test_shadow_mode_read_only.py's AST-verified
invariant) -- adding this registration does not change that. It is
permanently excluded from scheduling via
agents.runtime.scheduling_control.NEVER_SCHEDULABLE_AGENTS, the same
hard, code-level lock quant_researcher carries, so
run_agent_cycle("shadow_mode", ...) is never invoked automatically
regardless of what its cycle function contains -- only a human or a
test calling it directly. Also NOT added to
agents.sys_admin.orchestrator.AGENT_NAMES, for the same reason
trading_intelligence isn't -- always considered enabled here.

Milestone 17: trading_intelligence was removed from
NEVER_SCHEDULABLE_AGENTS -- it no longer carries that lock. This
docstring's own description of _trading_intelligence_cycle() above
(same contract as every other entry, invokes the existing, already-
tested run_scheduled_cycle() entrypoint) is unchanged by that -- M17
only concerns whether the SCHEDULER is permitted to call it on a
timer, not what the call itself does once made.
"""
import datetime as dt
import logging
import time

from .. import audit_log as audit_log_module
from .. import config, memory
from ..quant_researcher import research_engine
from ..risk_manager import api as risk_api
from ..shadow_mode import api as shadow_api
from ..sys_admin import orchestrator, self_healing, sysadmin_report, sysadmin_store
from ..sys_admin.admin_agent import SystemAdministrator
from ..trading_intelligence import api as ti_api
from ..trading_intelligence import structure_alerts
from ..trading_supervisor import agent_health
from ..trading_supervisor.supervisor_agent import TradingSupervisor
from . import market_session, runtime_events, task_queue

logger = logging.getLogger("oi_dashboard.runtime.agent_runtime")

RUNTIME_AGENT_NAMES = (
    "memory", "dev_agent", "quant_researcher", "risk_manager", "trading_supervisor", "sys_admin",
    "trading_intelligence",  # wired in during the Milestone 10 final review pass -- see below
    "shadow_mode",  # Milestone 12, Phase 3 -- status/health tracking only, see module docstring above
)


def _memory_cycle(store, *, repo_dir: str) -> list:
    health = agent_health.memory_health(store)
    if not health.reachable:
        raise RuntimeError("memory store unreachable")
    if health.is_stale:
        return [{"summary": "memory store reachable but no recent writes (may be benign)", "severity": "warning"}]
    return [{"summary": "memory store healthy", "severity": "info"}]


def _dev_agent_cycle(store, *, repo_dir: str) -> list:
    from ..dev_agent import pipeline

    def handler(payload):
        pipeline.run_proposal(
            repo_dir, payload["trigger"], payload["target_files"],
            base_ref=payload.get("base_ref", "main"), memory_store=store,
        )

    result = task_queue.process_one({"dev_agent_trigger": handler})
    if result is None:
        return [{"summary": "no dev_agent_trigger tasks queued", "severity": "info"}]
    return [{"summary": f"processed queued trigger: {result}", "severity": "info"}]


def _quant_researcher_cycle(store, *, repo_dir: str) -> list:
    import backtest

    findings = []
    for symbol in config.RUNTIME_RESEARCH_SYMBOLS:
        candles = backtest.load_intraday_candles(symbol)
        if candles.empty:
            findings.append({"summary": f"no candle archive for {symbol}", "severity": "info"})
            continue
        date_to = candles["datetime"].max().date()
        date_from = date_to - dt.timedelta(days=config.RUNTIME_RESEARCH_LOOKBACK_DAYS)
        result = research_engine.run_research_cycle(
            repo_dir, symbol, date_from=date_from.isoformat(), date_to=date_to.isoformat(), memory_store=store,
        )
        findings.append({
            "summary": f"{symbol}: {result.hypotheses_tested} hypotheses tested, "
                       f"{len(result.validated)} validated, promoted={result.promoted}",
            "severity": "info",
        })
    return findings


def _risk_manager_cycle(store, *, repo_dir: str) -> list:
    snapshot = risk_api.get_portfolio_snapshot(user_id=None, persist=True)
    if snapshot["details"].get("available") is False:
        raise RuntimeError(f"portfolio snapshot unavailable: {snapshot['details'].get('reason')}")
    return [{"summary": snapshot["summary"], "severity": "info"}]


def _trading_supervisor_cycle(store, *, repo_dir: str) -> list:
    kwargs = {"memory_store": store}
    if config.RUNTIME_SUPERVISOR_SYMBOLS is not None:
        kwargs["watched_symbols"] = config.RUNTIME_SUPERVISOR_SYMBOLS
    agent = TradingSupervisor(audit_log_module, **kwargs)
    return agent.run_cycle()


def _sys_admin_cycle(store, *, repo_dir: str) -> list:
    agent = SystemAdministrator(audit_log_module, memory_store=store, repo_dir=repo_dir)
    return agent.run_cycle()


def _trading_intelligence_cycle(store, *, repo_dir: str) -> list:
    """Milestone 10's paper-trading-only engine, run unattended -- see
    agents.trading_intelligence.api.run_scheduled_cycle()'s own docstring
    for exactly what one cycle does. Market-hours gated -- but, as of
    Milestone 19+, PER-SYMBOL rather than the single NSE-only gate
    agents.runtime.scheduler._MARKET_SESSION_GATED_AGENTS used to apply
    uniformly: config.TI_WATCHED_SYMBOLS mixes NSE indexes and MCX
    commodities with genuinely different session hours, and gating the
    whole engine on NSE hours alone meant it never ran at all during a
    genuine MCX-only evening session. scheduler.py's own due-agent check
    already confirmed at least one exchange is open before invoking this
    at all; this function re-derives the actual open subset (cheap, pure
    stdlib datetime math, no broker call) so ti_api.run_scheduled_cycle()
    only ever evaluates symbols whose exchange is open RIGHT NOW -- an
    NSE symbol's stale post-close cycle data is never evaluated during
    MCX-only hours, and vice versa. Never touches the broker -- the
    package's own __init__.py safety rule and test_safety.py's AST scan
    cover this module exactly like every other trading_intelligence
    entrypoint."""
    active = market_session.active_symbols(config.TI_WATCHED_SYMBOLS)
    logger.info(
        "trading_intelligence cycle",
        extra={
            "active_symbols": active,
            "nse_open": market_session.is_nse_session_open()[0],
            "mcx_open": market_session.is_mcx_session_open()[0],
        },
    )
    if not active:
        return [{"summary": "trading_intelligence skipped -- no active market sessions", "severity": "info"}]
    results = ti_api.run_scheduled_cycle(symbols=active)
    findings = []
    for symbol, r in results.items():
        if not r["available"]:
            findings.append({"summary": f"{symbol}: unavailable -- {r['reason']}", "severity": "info"})
        elif r["trade_opened"]:
            findings.append({"summary": f"{symbol}: {r['action']} -- paper trade #{r['trade_id']} opened",
                              "severity": "info"})
        else:
            findings.append({"summary": f"{symbol}: {r['action']}", "severity": "info"})

    # Milestone 20, Phase 2: structure-alert-only evaluation -- a
    # SEPARATE step from everything above, never touching
    # ai_trading_engine.evaluate()/paper_trading.enter_from_recommendation()
    # or their results. No-ops entirely (module-level, before any
    # candle/OI fetch) unless config.TI_ENABLE_STRUCTURE_ALERTS is
    # explicitly true. Wrapped in try/except so a bug here can never
    # break the real signal/paper-trade pipeline above -- same
    # "observability/side-channel failure must never defeat the real
    # action" contract runtime_events.emit_safe() already established.
    try:
        structure_alerts.run_structure_alert_cycle(symbols=active)
    except Exception as exc:
        logger.warning(f"structure_alerts.run_structure_alert_cycle() failed (non-fatal, real cycle unaffected): {exc}")

    return findings


def _shadow_mode_cycle(store, *, repo_dir: str) -> list:
    """Milestone 12, Phase 3: Shadow Mode's runtime-registry presence --
    a READ-ONLY status/health heartbeat ONLY. Deliberately never calls
    agents.shadow_mode.observer.observe_and_predict() or evaluator.
    evaluate_pending() -- shadow_mode_cli.py remains the ONLY caller of
    either, anywhere in this codebase. This function exists purely so
    Shadow Mode participates in the same health-tracking/status-
    reporting system every other runtime agent does; it is permanently
    excluded from scheduling (agents.runtime.scheduling_control.
    NEVER_SCHEDULABLE_AGENTS), so this only ever runs via a direct
    run_agent_cycle("shadow_mode", ...) call (a human or a test), never
    automatically."""
    status = shadow_api.get_status()
    if status["observation_count"] == 0:
        return [{
            "summary": "Shadow Mode reachable -- no observations recorded yet "
                       "(expected until an operator runs shadow_mode_cli.py observe)",
            "severity": "info",
        }]
    return [{
        "summary": f"Shadow Mode reachable -- {status['observation_count']} observation(s), "
                   f"{status['prediction_count']} prediction(s) recorded, "
                   f"last prediction at {status['last_prediction_ts']}",
        "severity": "info",
    }]


_CYCLE_FUNCS = {
    "memory": _memory_cycle,
    "dev_agent": _dev_agent_cycle,
    "quant_researcher": _quant_researcher_cycle,
    "risk_manager": _risk_manager_cycle,
    "trading_supervisor": _trading_supervisor_cycle,
    "sys_admin": _sys_admin_cycle,
    "trading_intelligence": _trading_intelligence_cycle,
    "shadow_mode": _shadow_mode_cycle,
}


def _escalate(agent: str, status: dict, error: str) -> None:
    report = sysadmin_report.build(
        module="agent_runtime", action="escalate",
        reason=f"{agent} failed {status['failure_counter']} consecutive cycles "
               f"(>= {config.RUNTIME_MAX_CONSECUTIVE_FAILURES_BEFORE_ESCALATION}) -- automatic restart "
               f"alone is not resolving it; escalating for human attention",
        confidence=90, evidence={"agent": agent, "failure_counter": status["failure_counter"], "last_error": error},
        affected_components=[agent], severity="critical",
    )
    sysadmin_store.record_report(report)
    runtime_events.emit("agent_runtime", runtime_events.AGENT_ESCALATED, {
        "agent": agent, "failure_counter": status["failure_counter"], "error": error,
    })


def run_agent_cycle(agent: str, *, memory_store=None, repo_dir: str = ".") -> dict:
    """Runs ONE cycle for `agent`. Never raises -- a failing agent must
    never take down the scheduler calling this in a loop; the failure
    itself is exactly what gets recorded, escalated, and (for the one
    safe case) auto-healed."""
    if agent not in RUNTIME_AGENT_NAMES:
        raise ValueError(f"agent must be one of {RUNTIME_AGENT_NAMES}, got {agent!r}")

    # orchestrator.py's own enable/disable flag (Milestone 8) only
    # tracks the four agents in orchestrator.AGENT_NAMES -- "memory" and
    # "sys_admin" were deliberately excluded there (see that module's
    # docstring) and are always considered enabled here.
    orchestrator_agent = agent if agent in orchestrator.AGENT_NAMES else None
    if orchestrator_agent and not orchestrator.is_enabled(orchestrator_agent):
        return {"agent": agent, "success": None, "skipped": "disabled"}

    store = memory_store or memory.get_memory_store()
    sysadmin_store.set_currently_running(agent, True)
    if orchestrator_agent:
        orchestrator.heartbeat(orchestrator_agent)

    start = time.perf_counter()
    try:
        findings = _CYCLE_FUNCS[agent](store, repo_dir=repo_dir)
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        status = sysadmin_store.record_execution(agent, success=True, duration_ms=duration_ms)
        return {"agent": agent, "success": True, "duration_ms": duration_ms, "findings": findings, "status": status}
    except Exception as exc:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        status = sysadmin_store.record_execution(agent, success=False, duration_ms=duration_ms)
        runtime_events.emit("agent_runtime", runtime_events.AGENT_CYCLE_FAILED, {"agent": agent, "error": str(exc)})
        if orchestrator_agent:
            orchestrator.record_crash(orchestrator_agent, reason=str(exc))
            self_healing.heal_agent_crash(orchestrator_agent)  # the one automatic recovery: clears the flag
        if status["failure_counter"] >= config.RUNTIME_MAX_CONSECUTIVE_FAILURES_BEFORE_ESCALATION:
            _escalate(agent, status, str(exc))
        return {"agent": agent, "success": False, "duration_ms": duration_ms, "error": str(exc), "status": status}


def health_snapshot() -> dict:
    """Every runtime agent's current status row, keyed by name --
    what the runtime dashboard displays."""
    return {agent: sysadmin_store.get_agent_status(agent) for agent in RUNTIME_AGENT_NAMES}
