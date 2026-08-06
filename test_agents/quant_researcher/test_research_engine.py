"""
test_agents/quant_researcher/test_research_engine.py -- end-to-end
regression tests for run_research_cycle(). The genuinely uncertain/
expensive boundaries are mocked (agents.quant_researcher.hypotheses.
generate_hypotheses -- so the test controls exactly which StrategySpec
gets backtested, instead of depending on real catalog feature math
firing a predictable number of times; agents.quant_researcher.
data_access -- so no real backtest.py/oi_history.db import happens);
everything downstream of that (strategy_runner's real simulation,
statistics_validation, evolution, promotion, and -- when a candidate is
actually promoted -- a REAL git worktree run through the real five gates
against a toy_repo) is exercised for real, same testing philosophy as
test_agents/dev_agent/test_pipeline.py's happy-path tests.
"""
import pandas as pd
import pytest

from agents.dev_agent import pipeline as dev_pipeline, worktree
from agents.dev_agent.gates.base import GateResult, GateStatus
from agents.memory.sqlite_store import SQLiteMemoryStore
from agents.quant_researcher import data_access, hypotheses, research_engine
from agents.quant_researcher.strategy_spec import StrategySpec


def _trending_candles(n=400, drift=0.5, amplitude=0.3, start="2026-05-04 09:15:00"):
    rows = []
    ts = pd.Timestamp(start)
    price = 100.0
    for i in range(n):
        price += drift
        wiggle = amplitude if i % 2 == 0 else -amplitude
        o, c = price, price + wiggle
        rows.append({
            "datetime": ts + pd.Timedelta(minutes=3 * i),
            "open": o, "high": max(o, c) + 0.5, "low": min(o, c) - 0.5, "close": c, "volume": 100,
        })
        price = c
    return pd.DataFrame(rows)


def _always_long_spec(symbol="NIFTY", **overrides):
    base = dict(
        name=f"always_long_{symbol}", symbol=symbol, hypothesis_id="oi_delta_combo", features=["atr"],
        thresholds={"atr": -1.0}, direction="long", target_points=2.0, stop_points=100.0, max_hold_bars=5,
        params={"invert": {}},
    )
    base.update(overrides)
    return StrategySpec(**base)


def _never_trades_spec(symbol="NIFTY"):
    return StrategySpec(
        name=f"never_{symbol}", symbol=symbol, hypothesis_id="iv_crush", features=["atr"],
        thresholds={"atr": 999999.0}, direction="long", target_points=2.0, stop_points=5.0,
        max_hold_bars=5, params={"invert": {}},
    )


def _weak_baseline():
    return {"net_pnl": 0.0, "profit_factor": 1.0, "sharpe_ratio": 0.05, "expectancy": 0.05,
            "recovery_factor": 0.5, "max_drawdown": 1000.0, "total_trades": 10}


def _strong_baseline():
    return {"net_pnl": 1_000_000.0, "profit_factor": 50.0, "sharpe_ratio": 10.0, "expectancy": 500.0,
            "recovery_factor": 50.0, "max_drawdown": 1.0, "total_trades": 500}


def _patch_data_access(monkeypatch, *, candles, baseline_stats, cycles=None):
    monkeypatch.setattr(data_access, "load_candles", lambda symbol, **k: candles)
    monkeypatch.setattr(data_access, "load_cycles_for_range", lambda *a, **k: cycles or [])
    monkeypatch.setattr(data_access, "production_baseline_stats", lambda *a, **k: baseline_stats)


def _patch_all_gates_pass(monkeypatch):
    def _passing(name):
        return GateResult(gate=name, status=GateStatus.PASSED, summary="synthetic pass")

    monkeypatch.setattr(dev_pipeline.unit_tests, "run", lambda path: _passing("unit_tests"))
    monkeypatch.setattr(dev_pipeline.integration_tests, "run", lambda path: _passing("integration_tests"))
    monkeypatch.setattr(
        dev_pipeline.backtest_compare, "run",
        lambda base, cand, files: GateResult(gate="backtest_compare", status=GateStatus.SKIPPED, summary="skip"),
    )
    monkeypatch.setattr(
        dev_pipeline.benchmark, "run",
        lambda bt: GateResult(gate="benchmark", status=GateStatus.SKIPPED, summary="nothing to benchmark"),
    )
    monkeypatch.setattr(dev_pipeline.code_quality, "run", lambda path: _passing("code_quality"))


class TestMemoryResearchContext:
    def test_searches_all_three_new_memory_categories(self, tmp_path):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "mem.db"))
        store.record_market_regime(symbol="NIFTY", regime_type="Trending")
        store.record_trade_journal(symbol="NIFTY", entry_price=100, learning="be patient")
        store.record_institutional_pattern(symbol="NIFTY", pattern_type="GammaTrap", description="x")

        ctx = research_engine._memory_research_context(store, "NIFTY", 10)

        assert len(ctx["market_regime"]) == 1
        assert len(ctx["trade_journal"]) == 1
        assert len(ctx["institutional_patterns"]) == 1

    def test_already_failed_hypothesis_ids_matches_catalog_ids_in_description(self):
        failed = [{"description": "iv_crush: iv_crush_NIFTY", "reason": "not significant"}]
        ids = research_engine._already_failed_hypothesis_ids(failed)
        assert ids == {"iv_crush"}


class TestRunResearchCycleNoValidHypothesis:
    def test_no_trades_means_no_promotion_and_a_failed_experiment_recorded(self, tmp_path, monkeypatch):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "mem.db"))
        _patch_data_access(monkeypatch, candles=_trending_candles(), baseline_stats=_weak_baseline())
        monkeypatch.setattr(hypotheses, "generate_hypotheses", lambda **k: [_never_trades_spec()])

        result = research_engine.run_research_cycle(
            "/unused", "NIFTY", date_from="2026-05-04", date_to="2026-05-06", memory_store=store,
        )

        assert result.promoted is False
        assert "no hypothesis passed" in result.promotion_reasoning
        failed = store.search_failed_experiments("research_cycle:NIFTY")
        assert len(failed) == 1


class TestRunResearchCycleValidatedButNotPromoted:
    def test_beats_no_baseline_metric_so_it_is_archived_not_promoted(self, tmp_path, monkeypatch):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "mem.db"))
        _patch_data_access(monkeypatch, candles=_trending_candles(), baseline_stats=_strong_baseline())
        monkeypatch.setattr(hypotheses, "generate_hypotheses", lambda **k: [_always_long_spec()])

        result = research_engine.run_research_cycle(
            "/unused", "NIFTY", date_from="2026-05-04", date_to="2026-05-06", memory_store=store,
        )

        assert result.promoted is False
        assert result.pipeline_result is None
        archived = store.search_parameter_sets(strategy_name="oi_delta_combo", symbol="NIFTY")
        assert any(p["is_best"] == 0 for p in archived)


class TestRunResearchCyclePromotion:
    def test_full_cycle_promotes_and_routes_through_the_seven_gates(self, agent_db, tmp_path, toy_repo, monkeypatch):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "mem.db"))
        _patch_data_access(monkeypatch, candles=_trending_candles(), baseline_stats=_weak_baseline())
        monkeypatch.setattr(hypotheses, "generate_hypotheses", lambda **k: [_always_long_spec()])
        _patch_all_gates_pass(monkeypatch)
        # Milestone 7's supervision gate reads live-ish market state/data-feed
        # health (backtest.load_market_structure_snapshots / load_cycles) --
        # already-degrade-to-"unknown" on a failure (see market_state.py/
        # data_health.py), but an "unknown" market-state dimension pushes the
        # verdict to REQUIRES_REVIEW, not APPROVED. Stub both to a clean,
        # fully-known state so this test's APPROVED expectation is
        # deterministic rather than depending on this toy_repo's oi_history.db
        # happening to have real market-structure/cycle data for "today."
        from agents.trading_supervisor import data_health, market_state
        monkeypatch.setattr(market_state, "assess", lambda symbol, date, **k: market_state.MarketState(
            symbol=symbol, date=date, trend_range={"regime": "Trending", "adx": 30.0, "atr_14": 10.0},
            volatility={"level": "normal", "vix": 14.0, "percentile": 50.0},
            expiry={"status": "normal"}, event={"status": "normal"},
        ))
        monkeypatch.setattr(data_health, "check_feed_staleness", lambda symbol, **k: data_health.DataHealth(
            symbol=symbol, latest_cycle_ts="t", staleness_minutes=1.0, is_stale=False, note="fresh",
        ))

        before = len(worktree.list_worktrees(repo_dir=str(toy_repo)))
        result = research_engine.run_research_cycle(
            str(toy_repo), "NIFTY", date_from="2026-05-04", date_to="2026-05-06",
            memory_store=store, base_ref="main",
        )
        after = len(worktree.list_worktrees(repo_dir=str(toy_repo)))

        assert result.promoted is True
        assert result.pipeline_result.decision.value == "APPROVED"
        assert after == before + 1  # worktree kept, not rolled back, on approval

        # Milestones 6 + 7: the original five gates plus the Promotion Risk
        # Gate and the Trading Supervision gate, in that fixed order.
        gate_names = [g.gate for g in result.pipeline_result.gate_results]
        assert gate_names.count("risk_assessment") == 1
        assert gate_names.count("trading_supervision") == 1
        assert gate_names[-2:] == ["risk_assessment", "trading_supervision"]

        best = store.search_parameter_sets(strategy_name="oi_delta_combo", symbol="NIFTY")
        assert any(p["is_best"] == 1 for p in best)
        assert best[0]["parameters"]["direction"] == "long"  # Milestone 7: direction now stored for conflict detection
        evolutions = store.search_strategy_evolution(strategy_name="oi_delta_combo")
        assert len(evolutions) >= 2  # one from optimize_parameters, one from the promotion itself
        backtests = store.list_backtest_history(symbol="NIFTY")
        assert len(backtests) == 1
        assert backtests[0]["trades"]  # Milestone 6: real trades persisted, not just aggregate stats

        from agents.trading_supervisor import supervision_store
        supervision_rows = supervision_store.list_supervision_log(symbol="NIFTY")
        assert len(supervision_rows) == 1
        assert supervision_rows[0]["decision"] == "APPROVED"

        from agents.risk_manager import risk_store
        assessments = risk_store.list_assessments(symbol="NIFTY")
        assert len(assessments) == 1
        assert assessments[0]["decision"] in ("APPROVED", "REQUIRES_REVIEW")

    def test_gate_rejection_rolls_back_and_records_failed_experiment(self, agent_db, tmp_path, toy_repo, monkeypatch):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "mem.db"))
        _patch_data_access(monkeypatch, candles=_trending_candles(), baseline_stats=_weak_baseline())
        monkeypatch.setattr(hypotheses, "generate_hypotheses", lambda **k: [_always_long_spec()])
        _patch_all_gates_pass(monkeypatch)
        monkeypatch.setattr(
            dev_pipeline.unit_tests, "run",
            lambda path: GateResult(gate="unit_tests", status=GateStatus.FAILED, summary="synthetic failure"),
        )

        before = len(worktree.list_worktrees(repo_dir=str(toy_repo)))
        result = research_engine.run_research_cycle(
            str(toy_repo), "NIFTY", date_from="2026-05-04", date_to="2026-05-06",
            memory_store=store, base_ref="main",
        )
        after = len(worktree.list_worktrees(repo_dir=str(toy_repo)))

        assert result.promoted is False
        assert result.pipeline_result.decision.value == "REJECTED"
        assert after == before  # rolled back
        failed = store.search_failed_experiments("quant_researcher_promotion:NIFTY")
        assert len(failed) == 1
