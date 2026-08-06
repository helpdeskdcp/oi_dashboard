"""
test_agents/quant_researcher/test_evolution.py -- regression tests for
the parameter-optimisation / feature-selection / evolution-recording
functions. Uses a real (in-process) strategy_runner.run_strategy against
synthetic candles -- no monkeypatching needed since none of this touches
backtest.py.
"""
import pandas as pd

from agents.memory.sqlite_store import SQLiteMemoryStore
from agents.quant_researcher import evolution
from agents.quant_researcher.strategy_spec import StrategySpec


def _candles(prices, *, start="2026-05-04 09:15:00"):
    rows = []
    ts = pd.Timestamp(start)
    for i, p in enumerate(prices):
        rows.append({
            "datetime": ts + pd.Timedelta(minutes=3 * i),
            "open": p, "high": p + 1, "low": p - 1, "close": p, "volume": 100,
        })
    return pd.DataFrame(rows)


def _spec(**overrides):
    base = dict(
        name="evo_test", symbol="NIFTY", hypothesis_id="oi_delta_combo", features=["atr"],
        thresholds={"atr": -1.0}, direction="long", target_points=5.0, stop_points=5.0,
        max_hold_bars=5, params={},
    )
    base.update(overrides)
    return StrategySpec(**base)


class TestOptimizeParameters:
    def test_grid_search_returns_best_first_and_respects_cap(self, monkeypatch):
        from agents import config
        monkeypatch.setattr(config, "QUANT_RESEARCH_MAX_GRID_COMBINATIONS", 4)
        candles = _candles([100.0] * 5 + [120.0] * 15)
        spec = _spec()
        best_spec, best_stats, all_results = evolution.optimize_parameters(
            spec, candles, threshold_grid={"atr": [-1.0, -0.5]},
            target_stop_grid=[(5.0, 50.0), (50.0, 50.0)],
        )
        assert len(all_results) <= 4
        # results sorted best-first by (sharpe_ratio, profit_factor)
        scores = [evolution._score(stats) for _spec_, stats in all_results]
        assert scores == sorted(scores, reverse=True)

    def test_empty_candles_degrades_to_original_spec(self):
        spec = _spec()
        best_spec, best_stats, results = evolution.optimize_parameters(spec, pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close"]
        ))
        assert best_stats["total_trades"] == 0


class TestSelectFeatures:
    def test_single_feature_spec_is_returned_unchanged(self):
        candles = _candles([100.0] * 20)
        spec = _spec(features=["atr"])
        best_spec, best_stats, results = evolution.select_features(spec, candles)
        assert best_spec.features == ["atr"]

    def test_two_feature_spec_tries_dropping_each_feature(self):
        candles = _candles([100.0] * 20)
        spec = _spec(features=["atr", "vwap_deviation"], thresholds={"atr": -1.0, "vwap_deviation": 999.0})
        best_spec, best_stats, results = evolution.select_features(spec, candles)
        candidate_feature_sets = {tuple(c.features) for c, _s in results}
        assert ("atr",) in candidate_feature_sets
        assert ("vwap_deviation",) in candidate_feature_sets
        assert ("atr", "vwap_deviation") in candidate_feature_sets


class TestRecordEvolutionStep:
    def test_writes_to_strategy_evolution_memory(self, tmp_path):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "mem.db"))
        spec = _spec()
        row_id = evolution.record_evolution_step(
            store, spec, {"sharpe_ratio": 1.2}, change_summary="test optimisation pass",
        )
        assert row_id == 1
        hits = store.search_strategy_evolution(strategy_name=spec.hypothesis_id)
        assert len(hits) == 1
        assert hits[0]["change_summary"] == "test optimisation pass"
