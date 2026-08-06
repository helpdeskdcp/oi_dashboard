"""
agents/quant_researcher/research_engine.py -- orchestrates one full
research cycle: search memory -> generate hypotheses -> backtest ->
statistically validate -> evolve -> compare to the current production
baseline -> promote (through the SAME five-gate pipeline
agents.dev_agent uses) or archive.

Safety: never overwrites a production strategy file, and never merges
anything. Promotion only ever produces a new file, on a new branch,
inside an isolated worktree -- exactly like agents.dev_agent's own
proposals -- and an APPROVED decision from the gates still only means
"pending human approval" (see agents/dev_agent/approval_engine.py's own
docstring: no apply/execute method exists anywhere in this framework). A
human still acts via approve_cli.py.

_run_gates is imported from agents.dev_agent.pipeline deliberately, not
reimplemented -- "every promoted strategy must require approval through
the existing five-gate pipeline" means literally the same gate sequence
dev_agent already runs, not a second implementation of what counts as
passing.
"""
import dataclasses
import os
import subprocess

from .. import audit_log, config, memory
from ..dev_agent import approval_engine, patch_generator, worktree
from ..dev_agent.pipeline import PipelineResult, _run_gates
from . import codegen, data_access, evolution, hypotheses, promotion, statistics_validation, strategy_runner

AGENT_NAME = "quant_researcher"


@dataclasses.dataclass
class HypothesisResult:
    spec: object
    stats: dict
    validation: object
    evolved: bool = False


@dataclasses.dataclass
class ResearchCycleResult:
    symbol: str
    hypotheses_tested: int
    validated: list
    promoted: bool
    promotion_reasoning: str
    pipeline_result: object = None
    audit_log_id: object = None


def _memory_research_context(store, symbol: str, limit: int) -> dict:
    """Requirement: "Use the new Memory System: Market Regime Memory,
    Trade Journal Memory, Institutional Pattern Memory. Search these
    before every research cycle." Also pulls prior failed_experiments so
    already-tried, already-failed hypotheses aren't retried unchanged --
    "the AI won't repeat the same mistakes"."""
    return {
        "market_regime": store.search_market_regime(symbol=symbol, limit=limit),
        "trade_journal": store.search_trade_journal(symbol=symbol, limit=limit),
        "institutional_patterns": store.search_institutional_pattern(symbol=symbol, limit=limit),
        "failed_experiments": store.search_failed_experiments(f"research_cycle:{symbol}", limit=limit),
        "prior_parameter_sets": store.search_parameter_sets(symbol=symbol, limit=limit),
    }


def _already_failed_hypothesis_ids(failed_experiments: list) -> set:
    ids = set()
    for exp in failed_experiments:
        haystack = f"{exp.get('description') or ''} {exp.get('reason') or ''}"
        for h in hypotheses.HYPOTHESIS_CATALOG:
            if h["id"] in haystack:
                ids.add(h["id"])
    return ids


def _window_candles(candles, date_from: str, date_to: str):
    if candles is None or candles.empty:
        return candles
    import pandas as pd
    start, end = pd.Timestamp(date_from), pd.Timestamp(date_to) + pd.Timedelta(days=1)
    return candles[(candles["datetime"] >= start) & (candles["datetime"] < end)].reset_index(drop=True)


def run_research_cycle(repo_dir: str, symbol: str, *, date_from: str, date_to: str,
                        target_points: float = 30.0, stop_points: float = 15.0,
                        max_hold_bars: int = 20, memory_store=None,
                        base_ref: str = "main") -> ResearchCycleResult:
    store = memory_store or memory.get_memory_store()
    research_context = _memory_research_context(store, symbol, config.MEMORY_SEARCH_LIMIT)
    already_failed = _already_failed_hypothesis_ids(research_context["failed_experiments"])

    candles = _window_candles(data_access.load_candles(symbol), date_from, date_to)
    cycles = data_access.load_cycles_for_range(symbol, date_from, date_to) or None

    specs = hypotheses.generate_hypotheses(
        symbol=symbol, target_points=target_points, stop_points=stop_points,
        max_hold_bars=max_hold_bars, exclude_ids=already_failed,
    )

    validated: list = []
    for spec in specs:
        trades = strategy_runner.run_strategy(spec, candles, cycles)
        validation = statistics_validation.validate(trades)

        if not validation.passed:
            store.record_failed_experiment(
                trigger=f"research_cycle:{symbol}",
                description=f"{spec.hypothesis_id}: {spec.name}",
                reason=validation.reason,
                parameters={"thresholds": spec.thresholds, "target_points": spec.target_points,
                            "stop_points": spec.stop_points},
            )
            continue

        evolved_spec, evolved_stats, _all = evolution.optimize_parameters(spec, candles, cycles)
        evolution.record_evolution_step(
            store, evolved_spec, evolved_stats,
            change_summary=f"parameter optimisation over {spec.hypothesis_id} on {symbol}",
        )
        validated.append(
            HypothesisResult(spec=evolved_spec, stats=evolved_stats, validation=validation, evolved=True)
        )

    validated.sort(key=lambda r: (r.stats.get("sharpe_ratio") or float("-inf")), reverse=True)

    if not validated:
        return ResearchCycleResult(
            symbol=symbol, hypotheses_tested=len(specs), validated=[],
            promoted=False, promotion_reasoning="no hypothesis passed statistical validation this cycle.",
        )

    best = validated[0]
    baseline_stats = data_access.production_baseline_stats(symbol, date_from, date_to)
    decision = promotion.decide_promotion(best.stats, baseline_stats, validation_passed=best.validation.passed)

    store.record_backtest(
        symbol=symbol, date_from=date_from, date_to=date_to, stats=best.stats, comparison=decision.comparison,
    )

    if not decision.should_promote:
        store.record_parameter_set(
            strategy_name=best.spec.hypothesis_id, symbol=symbol, parameters=best.spec.thresholds,
            performance=best.stats, is_best=False, notes=decision.reasoning,
        )
        return ResearchCycleResult(
            symbol=symbol, hypotheses_tested=len(specs), validated=validated,
            promoted=False, promotion_reasoning=decision.reasoning,
        )

    pipeline_result = _submit_for_approval(
        repo_dir, best.spec, base_ref=base_ref, memory_store=store, promotion_comparison=decision.comparison,
    )
    return ResearchCycleResult(
        symbol=symbol, hypotheses_tested=len(specs), validated=validated,
        promoted=pipeline_result.decision == approval_engine.Decision.APPROVED,
        promotion_reasoning=decision.reasoning, pipeline_result=pipeline_result,
        audit_log_id=pipeline_result.audit_log_id,
    )


def _submit_for_approval(repo_dir, spec, *, base_ref, memory_store, promotion_comparison):
    """Materializes `spec` into research_strategies/<name>.py (+ a
    generated test) inside a fresh worktree, then routes it through the
    exact same five gates and approval decision agents.dev_agent.pipeline
    uses. Never merges or touches master; a REJECTED decision rolls the
    worktree back and records a failed_experiment, exactly like a
    dev_agent proposal that fails its gates."""
    module_relpath, module_import_name, test_relpath = codegen.file_paths(
        spec, strategies_dir=config.QUANT_RESEARCH_STRATEGIES_DIR
    )
    wt = worktree.create(spec.name, repo_dir=repo_dir, base_ref=base_ref)
    try:
        strategies_dir_path = os.path.join(wt.path, config.QUANT_RESEARCH_STRATEGIES_DIR)
        os.makedirs(strategies_dir_path, exist_ok=True)
        init_path = os.path.join(strategies_dir_path, "__init__.py")
        if not os.path.exists(init_path):
            open(init_path, "w").close()

        with open(os.path.join(wt.path, module_relpath), "w") as fh:
            fh.write(codegen.generate_module(spec))
        with open(os.path.join(wt.path, test_relpath), "w") as fh:
            fh.write(codegen.generate_test(spec, module_import_name))

        subprocess.run(["git", "add", "-A"], cwd=wt.path, capture_output=True, text=True, check=True)
        subprocess.run(
            ["git", "commit", "-q", "-m", f"quant_researcher: promote {spec.name}"],
            cwd=wt.path, capture_output=True, text=True, check=True,
        )

        files = patch_generator.changed_files(wt.path, base_ref, wt.branch)
        self_mod = patch_generator.touches_guarded_path(files, config.SELF_MODIFICATION_GUARD_PREFIX)
        gate_results = [] if self_mod else _run_gates(wt.path, repo_dir, files)
        decision = approval_engine.decide(gate_results, self_modification_detected=self_mod)
        diff = patch_generator.generate(wt.path, base_ref, wt.branch)

        outcome = "rejected" if decision.decision == approval_engine.Decision.REJECTED else "pending_approval"
        row_id = audit_log.record(
            agent=AGENT_NAME, action_type="proposal",
            description=f"{decision.decision.value}: promote {spec.name} ({spec.hypothesis_id}, {spec.symbol})",
            risk_tier="hard_blocked" if self_mod else "needs_approval", outcome=outcome,
            payload={
                "branch": wt.branch, "base_ref": base_ref, "changed_files": files,
                "decision": decision.decision.value, "reasoning": decision.reasoning,
                "gates": [g.to_dict() for g in gate_results], "diff": diff,
                "promotion_comparison": promotion_comparison, "spec": dataclasses.asdict(spec),
            },
        )

        if not self_mod:
            if decision.decision == approval_engine.Decision.REJECTED:
                memory_store.record_failed_experiment(
                    trigger=f"quant_researcher_promotion:{spec.symbol}",
                    description=f"promotion of {spec.name} rejected by the five-gate pipeline",
                    reason=decision.reasoning, audit_log_id=row_id,
                )
            else:
                memory_store.record_parameter_set(
                    strategy_name=spec.hypothesis_id, symbol=spec.symbol, parameters=spec.thresholds,
                    performance=promotion_comparison, is_best=True, notes=f"promoted via audit_log #{row_id}",
                )
                memory_store.record_strategy_evolution(
                    strategy_name=spec.hypothesis_id, version_label=spec.name,
                    change_summary=f"promoted to {module_relpath}, pending human approval",
                    rationale=decision.reasoning, audit_log_id=row_id,
                )

        should_remove = decision.decision == approval_engine.Decision.REJECTED
        if should_remove:
            worktree.remove(wt)

        return PipelineResult(
            decision=decision.decision, reasoning=decision.reasoning, gate_results=gate_results,
            diff=diff, changed_files=files, audit_log_id=row_id, worktree_removed=should_remove,
        )
    except Exception:
        worktree.remove(wt)
        raise
