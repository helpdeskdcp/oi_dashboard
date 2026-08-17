"""
agents/trading_intelligence/trade_guardian_graph.py -- LangGraph
orchestration for the Smart Mythos Trade Guardian (SHADOW/ADVISORY ONLY).

SCOPE, matching the exact precedent set by signal_graph.py (Phase 2):
trade_guardian.evaluate_position() is already one coherent, independently
tested function covering data-quality/regime/trend/OI/volume/Greeks/
resistance/target-feasibility/reversal-risk/trade-health/recommendation --
decomposing it into 16 separate graph nodes each doing its own DB read
would duplicate logic this module already has correctly, not add real
observability. This graph therefore has THREE honest nodes: fetch the
registered plan, run the one real evaluation, and decide (deterministically,
never via an LLM) whether the result represents a meaningful state change
worth a Telegram alert -- plus a `risk_gate` node that is a genuine safety
check, not decoration: it re-verifies the SL was never widened and the
action is one of the allowed states, as defense-in-depth on top of
trade_guardian._dynamic_sl()'s own structural clamp.

No node here calls an LLM. No node invents a numerical market value --
every field in GraphState comes from trade_guardian.evaluate_position()'s
own real computation. Deterministic calculations remain authoritative;
LangGraph is orchestration only, exactly as required.

FAILURE ISOLATION (two layers, matching the exact fix applied to
signal_graph.py's own PR #14 review): the langgraph import itself is
wrapped in try/except at the one place this module is imported from (see
that call site's own comment), and every node here is wrapped by
_run_node() so one node's exception never stops the graph from reaching
END. run_shadow() below never raises to its caller.
"""
import logging
import time

from langgraph.graph import END, StateGraph
from typing_extensions import TypedDict

from . import telegram_notifier, trade_guardian, trade_guardian_store

log = logging.getLogger(__name__)

ALLOWED_ACTIONS = frozenset(trade_guardian.ACTIONS)


class GraphState(TypedDict, total=False):
    position_id: str
    broker_position: dict | None
    plan: dict | None
    result: object
    previous_state: dict | None
    notify: bool
    notify_reason: str
    node_latencies: dict
    node_errors: dict


def _run_node(name, fn):
    def wrapped(state: GraphState) -> GraphState:
        node_latencies = dict(state.get("node_latencies") or {})
        node_errors = dict(state.get("node_errors") or {})
        start = time.monotonic()
        try:
            updates = fn(state) or {}
        except Exception as e:
            node_errors[name] = str(e)
            updates = {}
            log.warning(f"trade_guardian_graph node {name!r} failed (isolated, graph continues): {e}")
        node_latencies[name] = round((time.monotonic() - start) * 1000, 2)
        return {**updates, "node_latencies": node_latencies, "node_errors": node_errors}
    return wrapped


def _n_fetch_plan(state: GraphState) -> dict:
    plan = trade_guardian_store.get_plan(state["position_id"])
    previous_state = trade_guardian_store.get_state(state["position_id"])
    return {"plan": plan, "previous_state": previous_state}


def _n_evaluate(state: GraphState) -> dict:
    result = trade_guardian.evaluate_position(state["position_id"], broker_position=state.get("broker_position"))
    return {"result": result}


def _n_risk_gate(state: GraphState) -> dict:
    """Defense-in-depth: re-verifies the evaluation's own output never
    violates the absolute rules (SL never widened beyond the original,
    action is one of the allowed states) before this result is ever used
    for a recommendation or notification. Not decoration -- if this ever
    fires, it means trade_guardian.evaluate_position()'s own structural
    clamp had a bug, and this node's override is the last safety net."""
    result = state.get("result")
    plan = state.get("plan")
    if result is None or plan is None:
        return {}
    if result.action not in ALLOWED_ACTIONS:
        log.warning(f"trade_guardian_graph risk_gate: action {result.action!r} not in ALLOWED_ACTIONS -- forcing HOLD")
        result.action = "HOLD"
        result.reason = "risk_gate override -- an unrecognized action was produced"
    is_ce = plan["direction"] == "CE"
    if result.smart_sl is not None:
        widened = (result.smart_sl < plan["original_sl"]) if is_ce else (result.smart_sl > plan["original_sl"])
        if widened:
            log.warning(
                f"trade_guardian_graph risk_gate: smart_sl {result.smart_sl} would WIDEN "
                f"original_sl {plan['original_sl']} for {state['position_id']} -- forcing back to original"
            )
            result.smart_sl = plan["original_sl"]
            result.sl_action = "KEEP"
            result.reason = "risk_gate override -- a widened SL was rejected, original SL restored"
    return {"result": result}


def _n_decide_notification(state: GraphState) -> dict:
    """A meaningful state change only -- never re-alerts every cycle the
    same action/health tier is still active (matching telegram_notifier.
    py's own structure-update dedup convention: re-alert only on a real
    change, not on every scheduler tick)."""
    result = state.get("result")
    previous = state.get("previous_state")
    if result is None:
        return {"notify": False, "notify_reason": "no result to notify about"}
    if previous is None:
        return {"notify": True, "notify_reason": "first evaluation for this position"}
    changed = (
        previous.get("action") != result.action
        or previous.get("trade_health_tier") != result.trade_health_tier
        or previous.get("smart_sl") != result.smart_sl
    )
    return {
        "notify": changed,
        "notify_reason": "action/health/SL changed since last cycle" if changed else "no meaningful change since last cycle",
    }


_GRAPH = None


def _build_graph():
    global _GRAPH
    if _GRAPH is not None:
        return _GRAPH
    graph = StateGraph(GraphState)
    graph.add_node("fetch_plan", _run_node("fetch_plan", _n_fetch_plan))
    graph.add_node("evaluate", _run_node("evaluate", _n_evaluate))
    graph.add_node("risk_gate", _run_node("risk_gate", _n_risk_gate))
    graph.add_node("decide_notification", _run_node("decide_notification", _n_decide_notification))
    graph.set_entry_point("fetch_plan")
    graph.add_edge("fetch_plan", "evaluate")
    graph.add_edge("evaluate", "risk_gate")
    graph.add_edge("risk_gate", "decide_notification")
    graph.add_edge("decide_notification", END)
    _GRAPH = graph.compile()
    return _GRAPH


def run_shadow(position_id: str, *, broker_position: dict | None = None) -> dict:
    """Runs the Trade Guardian graph for ONE registered position. Never
    raises -- any failure (graph construction, invoke, or a node not
    already isolated by _run_node) is caught here, matching
    signal_graph.run_shadow()'s own contract."""
    start = time.monotonic()
    try:
        app_graph = _build_graph()
        final_state = app_graph.invoke({"position_id": position_id, "broker_position": broker_position})
        result = final_state.get("result")
        out = {
            "position_id": position_id, "result": result,
            "notify": final_state.get("notify", False), "notify_reason": final_state.get("notify_reason"),
            "node_latencies": final_state.get("node_latencies") or {},
            "node_errors": final_state.get("node_errors") or {}, "error": None,
        }
    except Exception as e:
        log.warning(f"trade_guardian_graph shadow run failed for {position_id!r} (isolated): {e}")
        out = {
            "position_id": position_id, "result": None, "notify": False, "notify_reason": None,
            "node_latencies": {}, "node_errors": {}, "error": str(e),
        }
    out["total_latency_ms"] = round((time.monotonic() - start) * 1000, 2)
    return out


def run_shadow_cycle(broker_positions: list) -> list:
    """The graph-orchestrated equivalent of trade_guardian.
    run_trade_guardian_cycle() -- evaluates every registered position
    through run_shadow() (LangGraph orchestration + risk_gate + the
    notification decision) instead of calling evaluate_position()
    directly, reusing trade_guardian._match_broker_position() rather
    than a second copy of the matching logic. This is what the
    production shadow call site (app.py, gated by
    config.TI_ENABLE_TRADE_GUARDIAN_SHADOW) actually invokes."""
    results = []
    for plan in trade_guardian_store.list_plans():
        match = trade_guardian._match_broker_position(plan, broker_positions)
        results.append(run_shadow(plan["position_id"], broker_position=match))
    return results


def run_shadow_cycle_and_notify(broker_positions: list) -> list:
    """run_shadow_cycle() plus the actual Telegram delivery step for
    every result the graph's own notify decision flagged as a genuine
    state change. Fully testable without app.py or a broker call --
    `broker_positions` is caller-supplied, and telegram_notifier.
    send_trade_guardian_update() is itself never-raise/fire-and-forget
    (a send failure here is caught anyway, as defense-in-depth, so one
    bad Telegram call can never stop the rest of the cycle)."""
    results = run_shadow_cycle(broker_positions)
    for out in results:
        if not out.get("notify") or out.get("result") is None:
            continue
        try:
            plan = trade_guardian_store.get_plan(out["position_id"])
            if plan is None:
                continue
            payload = trade_guardian.build_telegram_payload(plan, out["result"])
            telegram_notifier.send_trade_guardian_update(payload)
        except Exception as e:
            log.warning(f"Trade Guardian Telegram notify failed for {out['position_id']!r} (isolated): {e}")
    return results
