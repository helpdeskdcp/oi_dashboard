"""
agents/runtime/workflow_engine.py -- "Create workflows like: Market Data
-> Research -> Backtest -> Risk -> Supervisor -> Human Approval (if
required) -> Execution -> Learning -> Memory Update. Every workflow must
be restartable."

Reuses agents.quant_researcher.research_engine.run_research_cycle() --
which already runs Research -> Backtest -> Risk (Gate 6, "risk_
assessment") -> Supervisor (Gate 7, "trading_supervision") as ONE atomic,
already-tested call, per the seven-gate pipeline built across Milestones
3/5/6/7 -- for that middle span, rather than re-invoking each gate
separately, which would be exactly the kind of reimplementation the
brief says not to do. The workflow's own stage history still records
each individual gate's result (see _record_pipeline_gates below), so the
diagram in AI_RUNTIME.md and the runtime dashboard both show the four
conceptual stages distinctly even though one function call produces
them.

Restartable: every stage transition is persisted (runtime_store.
runtime_workflow + runtime_workflow_history) BEFORE returning, with
enough state_json to resume from exactly where it left off -- never
in-memory-only progress. agents/runtime/scheduler.py calls resume() on
every workflow runtime_store.resumable_workflows() returns at startup,
so a scheduler restart never loses in-flight work.

SAFETY SCOPING (read this before changing EXECUTION or HUMAN_APPROVAL):
- HUMAN_APPROVAL here is a WORKFLOW-level checkpoint ("should this
  workflow's own recommendation be finalized"), COMPLETELY SEPARATE from
  agents.dev_agent/quant_researcher's own code/strategy-promotion
  pending_approval gate in agent_audit_log, which this module NEVER
  touches -- that gate is, and stays, human-only via approve_cli.py
  regardless of runtime policy. Nothing in this file ever calls
  agents.audit_log.set_outcome().
- EXECUTION never places any order, paper or live, under ANY policy
  including full_auto. It computes and records a position-sized
  RECOMMENDATION only (reusing position_sizing.compute_quantity, the
  same pure-math sizing agents.risk_manager.risk_engine already trusts)
  -- there is no safe, importable "place a paper trade" entrypoint
  outside app.py (see agents/runtime/policy_engine.py's own docstring
  for why app.py is never imported from agents/).
"""
import datetime as dt

from .. import config, memory
from ..quant_researcher import research_engine
from ..sys_admin import sysadmin_report, sysadmin_store
from . import policy_engine, runtime_events, runtime_store

STAGE_MARKET_DATA = "market_data"
STAGE_RESEARCH = "research"           # covers Research + Backtest + Risk + Supervisor -- see module docstring
STAGE_HUMAN_APPROVAL = "human_approval"
STAGE_EXECUTION = "execution"
STAGE_LEARNING = "learning"
STAGE_MEMORY_UPDATE = "memory_update"

STAGES = (
    STAGE_MARKET_DATA, STAGE_RESEARCH, STAGE_HUMAN_APPROVAL, STAGE_EXECUTION, STAGE_LEARNING, STAGE_MEMORY_UPDATE,
)

_GATE_TO_CONCEPTUAL_STAGE = {
    "unit_tests": "research", "integration_tests": "research", "code_quality": "research",
    "backtest_compare": "backtest", "benchmark": "backtest",
    "risk_assessment": "risk", "trading_supervision": "supervisor",
}


class WorkflowError(Exception):
    pass


def start(symbol: str, *, memory_store=None) -> int:
    """Creates a new workflow at STAGE_MARKET_DATA. Returns the workflow
    id. Refuses to start if the active policy is read_only/
    recommendation_only (pure observation -- nothing to advance through
    a multi-stage pipeline for) or emergency_stop (the global kill
    switch)."""
    if not policy_engine.can_start_workflow():
        raise WorkflowError(
            f"active policy {policy_engine.get_active_policy()!r} does not permit starting a new workflow"
        )
    workflow_id = runtime_store.create_workflow(
        workflow_type="promotion", symbol=symbol, first_stage=STAGE_MARKET_DATA,
        state={"symbol": symbol},
    )
    runtime_events.emit("workflow_engine", runtime_events.WORKFLOW_STAGE_ADVANCED,
                         {"workflow_id": workflow_id, "stage": STAGE_MARKET_DATA})
    return workflow_id


def _fail(workflow_id: int, *, stage: str, error: str) -> None:
    runtime_store.record_stage_transition(workflow_id, stage=stage, status="failed", detail={"error": error})
    runtime_store.update_workflow(workflow_id, status="failed")
    runtime_events.emit("workflow_engine", runtime_events.WORKFLOW_FAILED,
                         {"workflow_id": workflow_id, "stage": stage, "error": error})


def _complete(workflow_id: int, *, final_stage: str) -> None:
    runtime_store.update_workflow(workflow_id, current_stage=final_stage, status="completed")
    runtime_events.emit("workflow_engine", runtime_events.WORKFLOW_COMPLETED,
                         {"workflow_id": workflow_id, "final_stage": final_stage})


def _advance_market_data(workflow_id: int, state: dict) -> None:
    import backtest
    symbol = state["symbol"]
    candles = backtest.load_intraday_candles(symbol)
    if candles.empty:
        _fail(workflow_id, stage=STAGE_MARKET_DATA, error=f"no candle archive for {symbol}")
        return
    latest = candles["datetime"].max()
    state["latest_candle_ts"] = latest.isoformat()
    state["date_to"] = latest.date().isoformat()
    state["date_from"] = (latest.date() - dt.timedelta(days=config.RUNTIME_RESEARCH_LOOKBACK_DAYS)).isoformat()
    runtime_store.record_stage_transition(workflow_id, stage=STAGE_MARKET_DATA, status="completed",
                                           detail={"candles_available": len(candles), "latest": state["latest_candle_ts"]})
    runtime_store.update_workflow(workflow_id, current_stage=STAGE_RESEARCH, state=state)


def _record_pipeline_gates(workflow_id: int, pipeline_result) -> None:
    for gate in pipeline_result.gate_results:
        conceptual = _GATE_TO_CONCEPTUAL_STAGE.get(gate.gate, gate.gate)
        runtime_store.record_stage_transition(
            workflow_id, stage=conceptual, status=gate.status.value,
            detail={"gate": gate.gate, "summary": gate.summary},
        )


def _advance_research(workflow_id: int, state: dict, *, memory_store, repo_dir: str) -> None:
    symbol = state["symbol"]
    result = research_engine.run_research_cycle(
        repo_dir, symbol, date_from=state["date_from"], date_to=state["date_to"], memory_store=memory_store,
    )
    state["hypotheses_tested"] = result.hypotheses_tested
    state["validated_count"] = len(result.validated)

    if result.pipeline_result is None:
        # Zero validated hypotheses -- never reached the gate pipeline at
        # all. Not a failure of the workflow (a real, honest "nothing
        # cleared statistical validation this cycle"), so it completes
        # rather than fails.
        runtime_store.record_stage_transition(
            workflow_id, stage=STAGE_RESEARCH, status="completed",
            detail={"hypotheses_tested": result.hypotheses_tested, "validated": 0, "note": "no candidate to submit"},
        )
        runtime_store.update_workflow(workflow_id, state=state)
        _complete(workflow_id, final_stage=STAGE_RESEARCH)
        return

    state["audit_log_id"] = result.pipeline_result.audit_log_id
    state["decision"] = result.pipeline_result.decision.value
    _record_pipeline_gates(workflow_id, result.pipeline_result)

    if result.pipeline_result.decision.value == "REJECTED":
        runtime_store.update_workflow(workflow_id, state=state)
        _fail(workflow_id, stage=STAGE_RESEARCH, error=f"candidate rejected: {result.promotion_reasoning}")
        return

    runtime_store.update_workflow(workflow_id, current_stage=STAGE_HUMAN_APPROVAL, state=state)
    runtime_events.emit("workflow_engine", runtime_events.WORKFLOW_STAGE_ADVANCED,
                         {"workflow_id": workflow_id, "stage": STAGE_HUMAN_APPROVAL})


def _advance_human_approval(workflow_id: int, state: dict) -> str:
    """Returns 'advanced', 'waiting', or 'stopped'. See module docstring
    -- this is a WORKFLOW-level checkpoint, never touches
    agents.audit_log's own code/strategy pending_approval row."""
    if not policy_engine.can_reach_execution():
        runtime_store.record_stage_transition(
            workflow_id, stage=STAGE_HUMAN_APPROVAL, status="stopped",
            detail={"reason": f"policy {policy_engine.get_active_policy()!r} is observation-only"},
        )
        _complete(workflow_id, final_stage=STAGE_HUMAN_APPROVAL)
        return "stopped"

    if policy_engine.auto_approves():
        runtime_store.record_stage_transition(
            workflow_id, stage=STAGE_HUMAN_APPROVAL, status="auto_approved",
            detail={"policy": policy_engine.get_active_policy()},
        )
        runtime_store.update_workflow(workflow_id, current_stage=STAGE_EXECUTION)
        return "advanced"

    runtime_store.update_workflow(workflow_id, status="waiting_approval")
    runtime_events.emit("workflow_engine", runtime_events.WORKFLOW_WAITING_APPROVAL, {"workflow_id": workflow_id})
    return "waiting"


def approve_workflow(workflow_id: int, *, approved_by: str, reason: str = "") -> None:
    """A human (via approve_cli.py or the dashboard) unblocks a
    workflow parked at STAGE_HUMAN_APPROVAL with status='waiting_approval'."""
    wf = runtime_store.get_workflow(workflow_id)
    if wf is None:
        raise WorkflowError(f"no workflow with id={workflow_id}")
    if wf["status"] != "waiting_approval":
        raise WorkflowError(f"workflow {workflow_id} is not waiting for approval (status={wf['status']!r})")
    runtime_store.record_stage_transition(
        workflow_id, stage=STAGE_HUMAN_APPROVAL, status="approved",
        detail={"approved_by": approved_by, "reason": reason},
    )
    runtime_store.update_workflow(workflow_id, current_stage=STAGE_EXECUTION, status="running")
    runtime_events.emit("workflow_engine", runtime_events.APPROVAL_GRANTED,
                         {"workflow_id": workflow_id, "approved_by": approved_by})


def reject_workflow(workflow_id: int, *, rejected_by: str, reason: str) -> None:
    wf = runtime_store.get_workflow(workflow_id)
    if wf is None:
        raise WorkflowError(f"no workflow with id={workflow_id}")
    runtime_store.record_stage_transition(
        workflow_id, stage=STAGE_HUMAN_APPROVAL, status="rejected",
        detail={"rejected_by": rejected_by, "reason": reason},
    )
    runtime_store.update_workflow(workflow_id, status="cancelled")
    runtime_events.emit("workflow_engine", runtime_events.APPROVAL_REJECTED,
                         {"workflow_id": workflow_id, "rejected_by": rejected_by, "reason": reason})


def _advance_execution(workflow_id: int, state: dict) -> None:
    """Never places an order -- see module docstring. Computes a
    position-sized RECOMMENDATION only, reusing
    agents.risk_manager.risk_engine.position_sizing_check (the same
    pure sizing math the Live Portfolio Risk Monitor already trusts)."""
    from ..risk_manager import risk_engine
    check = risk_engine.position_sizing_check(15.0, capital=config.RISK_ACCOUNT_CAPITAL, risk_pct=1.0)
    recommendation = {
        "symbol": state["symbol"], "recommended_quantity": check.value, "sizing_detail": check.detail,
        "note": "RECOMMENDATION ONLY -- no order (paper or live) was placed. See workflow_engine.py's own "
                "module docstring for why this is a hard, structural limit, not a missing feature.",
    }
    state["execution_recommendation"] = recommendation
    runtime_store.record_stage_transition(workflow_id, stage=STAGE_EXECUTION, status="completed",
                                           detail=recommendation)
    runtime_store.update_workflow(workflow_id, current_stage=STAGE_LEARNING, state=state)


def _advance_learning(workflow_id: int, state: dict, *, memory_store) -> None:
    """Records what this workflow run learned back into Memory -- the
    same agents.memory.sqlite_store the six agents already read/write,
    never a parallel learning store."""
    note = (
        f"runtime workflow {workflow_id} for {state['symbol']}: "
        f"{state.get('hypotheses_tested', 0)} hypotheses tested, decision={state.get('decision', 'n/a')}"
    )
    memory_store.record_backtest(
        symbol=state["symbol"], date_from=state.get("date_from"), date_to=state.get("date_to"),
        stats={
            "hypotheses_tested": state.get("hypotheses_tested", 0),
            "validated": state.get("validated_count", 0),
            "decision": state.get("decision"), "note": note,
        },
        audit_log_id=state.get("audit_log_id"),
    )
    state["learning_note"] = note
    runtime_store.record_stage_transition(workflow_id, stage=STAGE_LEARNING, status="completed", detail={"note": note})
    runtime_store.update_workflow(workflow_id, current_stage=STAGE_MEMORY_UPDATE, state=state)


def _advance_memory_update(workflow_id: int, state: dict) -> None:
    runtime_store.record_stage_transition(workflow_id, stage=STAGE_MEMORY_UPDATE, status="completed", detail={})
    _complete(workflow_id, final_stage=STAGE_MEMORY_UPDATE)


def advance(workflow_id: int, *, memory_store=None, repo_dir: str = ".") -> str:
    """Runs exactly one stage transition from the workflow's CURRENT
    persisted stage -- safe to call repeatedly (a scheduler tick), and
    exactly what resume() calls after a restart. Returns the resulting
    workflow status."""
    if policy_engine.is_emergency_stop():
        runtime_store.record_stage_transition(
            workflow_id, stage="_policy", status="paused", detail={"reason": "emergency_stop active"}
        )
        return "running"  # left exactly where it was -- emergency_stop pauses, never cancels

    wf = runtime_store.get_workflow(workflow_id)
    if wf is None:
        raise WorkflowError(f"no workflow with id={workflow_id}")
    if wf["status"] in ("completed", "failed", "cancelled"):
        return wf["status"]
    if wf["status"] == "waiting_approval":
        return wf["status"]  # nothing to do until approve_workflow()/reject_workflow() is called

    store = memory_store or memory.get_memory_store()
    stage = wf["current_stage"]
    state = wf["state_json"]

    try:
        if stage == STAGE_MARKET_DATA:
            _advance_market_data(workflow_id, state)
        elif stage == STAGE_RESEARCH:
            _advance_research(workflow_id, state, memory_store=store, repo_dir=repo_dir)
        elif stage == STAGE_HUMAN_APPROVAL:
            _advance_human_approval(workflow_id, state)
        elif stage == STAGE_EXECUTION:
            _advance_execution(workflow_id, state)
        elif stage == STAGE_LEARNING:
            _advance_learning(workflow_id, state, memory_store=store)
        elif stage == STAGE_MEMORY_UPDATE:
            _advance_memory_update(workflow_id, state)
        else:
            raise WorkflowError(f"unknown stage {stage!r}")
    except Exception as exc:
        sysadmin_store.record_report(sysadmin_report.build(
            module="workflow_engine", action="advance",
            reason=f"workflow {workflow_id} raised at stage {stage!r}: {exc}",
            confidence=80, evidence={"workflow_id": workflow_id, "stage": stage, "error": str(exc)},
            severity="critical",
        ))
        _fail(workflow_id, stage=stage, error=str(exc))

    return runtime_store.get_workflow(workflow_id)["status"]


def resume(workflow_id: int, *, memory_store=None, repo_dir: str = ".") -> str:
    """Identical to advance() -- kept as a distinct name so callers
    (scheduler.py's startup recovery sweep) read as intent: "continue
    this workflow from wherever it was left," not "run its first
    stage.\""""
    return advance(workflow_id, memory_store=memory_store, repo_dir=repo_dir)


def run_to_completion(workflow_id: int, *, memory_store=None, repo_dir: str = ".", max_steps: int = 20) -> str:
    """Repeatedly advance()s until the workflow reaches a terminal
    status (completed/failed/cancelled) or pauses for
    waiting_approval/emergency_stop -- convenience for tests and for a
    scheduler tick that wants to drive one workflow all the way through
    in one pass rather than one stage per tick. max_steps is a hard
    circuit breaker -- STAGES has 6 entries; 20 is generous headroom,
    never an infinite loop even if a bug ever made advance() return
    'running' without moving the stage forward."""
    status = "running"
    for _ in range(max_steps):
        status = advance(workflow_id, memory_store=memory_store, repo_dir=repo_dir)
        if status != "running":
            return status
    return status
