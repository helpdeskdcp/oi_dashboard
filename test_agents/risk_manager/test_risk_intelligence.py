"""
test_agents/risk_manager/test_risk_intelligence.py -- regression tests
for the AI Risk Intelligence layer (agents/risk_manager/risk_intelligence.py).
Every test uses a real SQLiteMemoryStore against a tmp_path file --
never this repo's real oi_history.db.
"""
import datetime as dt

from agents.memory.sqlite_store import SQLiteMemoryStore
from agents.risk_manager import risk_intelligence


def _store(tmp_path):
    return SQLiteMemoryStore(db_path=str(tmp_path / "mem.db"))


class TestBuildActiveStrategies:
    def test_only_is_best_rows_are_active(self, tmp_path):
        store = _store(tmp_path)
        store.record_parameter_set(strategy_name="a", symbol="NIFTY", parameters={}, is_best=True)
        store.record_parameter_set(strategy_name="b", symbol="NIFTY", parameters={}, is_best=False)
        active = risk_intelligence.build_active_strategies(store)
        assert [a["strategy_name"] for a in active] == ["a"]

    def test_excludes_the_named_strategy(self, tmp_path):
        store = _store(tmp_path)
        store.record_parameter_set(strategy_name="a", symbol="NIFTY", parameters={}, is_best=True)
        store.record_parameter_set(strategy_name="b", symbol="NIFTY", parameters={}, is_best=True)
        active = risk_intelligence.build_active_strategies(store, exclude_strategy_name="a")
        assert [x["strategy_name"] for x in active] == ["b"]

    def test_pulls_real_trades_from_backtest_history_when_available(self, tmp_path):
        store = _store(tmp_path)
        store.record_parameter_set(strategy_name="a", symbol="NIFTY", parameters={}, is_best=True)
        store.record_backtest(symbol="NIFTY", date_from="x", date_to="y", stats={}, trades=[{"points": 5}])
        active = risk_intelligence.build_active_strategies(store)
        assert active[0]["trades"] == [{"points": 5}]

    def test_empty_trades_when_nothing_recorded(self, tmp_path):
        store = _store(tmp_path)
        store.record_parameter_set(strategy_name="a", symbol="NIFTY", parameters={}, is_best=True)
        active = risk_intelligence.build_active_strategies(store)
        assert active[0]["trades"] == []


class TestDetectFailurePatterns:
    def test_single_failure_is_not_a_repeated_pattern(self, tmp_path):
        store = _store(tmp_path)
        store.record_failed_experiment(trigger="t", description="oi_delta_combo: x", reason="bad")
        pattern = risk_intelligence.detect_failure_patterns(store, strategy_family="oi_delta_combo")
        assert pattern.occurrences == 1
        assert pattern.is_repeated is False

    def test_multiple_failures_are_a_repeated_pattern(self, tmp_path):
        store = _store(tmp_path)
        store.record_failed_experiment(trigger="t1", description="oi_delta_combo: a", reason="bad1")
        store.record_failed_experiment(trigger="t2", description="oi_delta_combo: b", reason="bad2")
        pattern = risk_intelligence.detect_failure_patterns(store, strategy_family="oi_delta_combo")
        assert pattern.occurrences == 2
        assert pattern.is_repeated is True
        assert set(pattern.reasons) == {"bad1", "bad2"}


class TestCompareWithHistoricalFailures:
    def test_similar_thresholds_are_flagged_as_known_bad(self, tmp_path):
        store = _store(tmp_path)
        store.record_failed_experiment(
            trigger="t", description="iv_crush: x", reason="regressed",
            parameters={"thresholds": {"iv_crush": -1.0}},
        )
        is_bad, matches, explanation = risk_intelligence.compare_with_historical_failures(
            store, strategy_family="iv_crush", candidate_thresholds={"iv_crush": -1.02},
        )
        assert is_bad is True
        assert len(matches) == 1
        assert "regressed" in explanation

    def test_dissimilar_thresholds_are_not_flagged(self, tmp_path):
        store = _store(tmp_path)
        store.record_failed_experiment(
            trigger="t", description="iv_crush: x", reason="regressed",
            parameters={"thresholds": {"iv_crush": -1.0}},
        )
        is_bad, matches, explanation = risk_intelligence.compare_with_historical_failures(
            store, strategy_family="iv_crush", candidate_thresholds={"iv_crush": -5.0},
        )
        assert is_bad is False
        assert matches == []

    def test_no_prior_failures_is_not_known_bad(self, tmp_path):
        store = _store(tmp_path)
        is_bad, matches, explanation = risk_intelligence.compare_with_historical_failures(
            store, strategy_family="brand_new", candidate_thresholds={"x": 1.0},
        )
        assert is_bad is False


class TestRecommendSaferParameters:
    def test_suggests_moving_away_from_the_failed_average(self):
        recs = risk_intelligence.recommend_safer_parameters(
            {"iv_crush": -1.0},
            [{"parameters": {"thresholds": {"iv_crush": -1.0}}}],
        )
        assert len(recs) == 1
        assert recs[0].parameter == "iv_crush"
        assert recs[0].suggested_value != recs[0].current_value

    def test_no_recommendation_for_keys_not_in_any_failure(self):
        recs = risk_intelligence.recommend_safer_parameters(
            {"unrelated_key": 5.0},
            [{"parameters": {"thresholds": {"iv_crush": -1.0}}}],
        )
        assert recs == []


class TestAssess:
    def _trades(self, n=40, points=10.0):
        return [
            {"points": points, "exit_time": dt.datetime(2026, 5, 4) + dt.timedelta(days=i)}
            for i in range(n)
        ]

    def test_clean_candidate_with_no_history_is_approved(self, tmp_path):
        store = _store(tmp_path)
        assessment = risk_intelligence.assess(
            store, candidate_name="c", symbol="NIFTY", strategy_family="oi_delta_combo",
            stop_points=15.0, trades=self._trades(), candidate_thresholds={"oi_delta_bias": 0.0},
            capital=1_000_000,
        )
        assert assessment.decision == "APPROVED"
        assert assessment.known_bad_configuration is False

    def test_known_bad_configuration_downgrades_approved_to_review(self, tmp_path):
        store = _store(tmp_path)
        store.record_failed_experiment(
            trigger="t", description="oi_delta_combo: x", reason="lost money",
            parameters={"thresholds": {"oi_delta_bias": 0.0}},
        )
        assessment = risk_intelligence.assess(
            store, candidate_name="c", symbol="NIFTY", strategy_family="oi_delta_combo",
            stop_points=15.0, trades=self._trades(), candidate_thresholds={"oi_delta_bias": 0.0},
            capital=1_000_000,
        )
        assert assessment.decision == "REQUIRES_REVIEW"
        assert assessment.known_bad_configuration is True
        assert "lost money" in assessment.explanation
        assert "Downgraded" in assessment.explanation

    def test_rejected_stays_rejected_even_with_known_bad_configuration(self, tmp_path, monkeypatch):
        from agents.risk_manager import risk_engine
        store = _store(tmp_path)
        store.record_failed_experiment(
            trigger="t", description="oi_delta_combo: x", reason="lost money",
            parameters={"thresholds": {"oi_delta_bias": 0.0}},
        )
        # The known-bad-configuration rule only ever softens an APPROVED
        # decision to REQUIRES_REVIEW -- it must never override a
        # REJECTED the pure risk math already produced for a harder
        # reason. Force that base decision deterministically rather than
        # relying on organically tripping enough checks.
        rejected = risk_engine.RiskAssessment(
            risk_score=10, decision="REJECTED", checks=[], var=0.0, cvar=0.0,
            drawdown_simulation={}, stress_test={}, correlations={}, explanation="base rejection",
        )
        monkeypatch.setattr(risk_engine, "evaluate_promotion", lambda **k: rejected)

        assessment = risk_intelligence.assess(
            store, candidate_name="c", symbol="NIFTY", strategy_family="oi_delta_combo",
            stop_points=15.0, trades=self._trades(), candidate_thresholds={"oi_delta_bias": 0.0},
            capital=1_000_000,
        )
        assert assessment.decision == "REJECTED"
        assert assessment.known_bad_configuration is True
