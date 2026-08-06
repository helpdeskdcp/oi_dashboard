"""
test_agents/memory/test_sqlite_store.py -- regression tests for
agents/memory/sqlite_store.py against a real throwaway SQLite file (never
this repo's real oi_history.db -- every test uses tmp_path).
"""
import pytest

from agents.memory.base import MemoryStore
from agents.memory.sqlite_store import SQLiteMemoryStore


@pytest.fixture()
def store(tmp_path):
    return SQLiteMemoryStore(db_path=str(tmp_path / "memory.db"))


class TestInterfaceCompliance:
    def test_sqlite_store_implements_memory_store(self, store):
        assert isinstance(store, MemoryStore)

    def test_memory_store_cannot_be_instantiated_directly(self):
        with pytest.raises(TypeError):
            MemoryStore()

    def test_init_db_is_idempotent(self, store):
        store.init_db()
        store.init_db()  # must not raise on a second call


class TestBugFixes:
    def test_record_and_search_round_trip(self, store):
        row_id = store.record_bug_fix(
            trigger="unit test failing", issue_summary="off-by-one in loop",
            root_cause="range() bound was wrong", fix_summary="fixed the bound",
            target_files=["backtest.py"], confidence_score=85, audit_log_id=42, outcome="approved",
        )
        assert row_id == 1

        hits = store.search_bug_fixes("off-by-one")
        assert len(hits) == 1
        assert hits[0]["issue_summary"] == "off-by-one in loop"
        assert hits[0]["fix_summary"] == "fixed the bound"
        assert hits[0]["target_files"] == ["backtest.py"]
        assert hits[0]["confidence_score"] == 85
        assert hits[0]["audit_log_id"] == 42
        assert hits[0]["outcome"] == "approved"

    def test_search_matches_by_target_file_even_without_text_overlap(self, store):
        store.record_bug_fix(
            trigger="t", issue_summary="unrelated wording", root_cause="r", fix_summary="f",
            target_files=["exit_engine_v4.py"],
        )
        hits = store.search_bug_fixes("nothing in common", target_files=["exit_engine_v4.py"])
        assert len(hits) == 1

    def test_search_with_no_matches_returns_empty(self, store):
        store.record_bug_fix(trigger="t", issue_summary="a", root_cause="b", fix_summary="c")
        assert store.search_bug_fixes("completely unrelated query xyz123") == []

    def test_search_orders_most_recent_first(self, store):
        store.record_bug_fix(trigger="t", issue_summary="first bug", root_cause="r", fix_summary="f")
        store.record_bug_fix(trigger="t", issue_summary="second bug", root_cause="r", fix_summary="f")
        hits = store.search_bug_fixes("bug")
        assert [h["issue_summary"] for h in hits] == ["second bug", "first bug"]

    def test_search_respects_limit(self, store):
        for i in range(5):
            store.record_bug_fix(trigger="t", issue_summary=f"bug {i}", root_cause="r", fix_summary="f")
        assert len(store.search_bug_fixes("bug", limit=2)) == 2

    def test_no_target_files_stored_as_none(self, store):
        store.record_bug_fix(trigger="t", issue_summary="a", root_cause="b", fix_summary="c")
        hits = store.search_bug_fixes("a")
        assert hits[0]["target_files"] is None


class TestFailedExperiments:
    def test_record_requires_a_reason(self, store):
        with pytest.raises(ValueError, match="reason"):
            store.record_failed_experiment(trigger="t", description="d", reason="")

    def test_record_and_search_round_trip(self, store):
        store.record_failed_experiment(
            trigger="tried raising the SL cap", description="widen SL cap to 50 points",
            reason="net P&L regressed 12% in backtest", target_files=["exit_engine_v4.py"],
            parameters={"sl_cap": 50}, audit_log_id=7,
        )
        hits = store.search_failed_experiments("SL cap")
        assert len(hits) == 1
        assert hits[0]["reason"] == "net P&L regressed 12% in backtest"
        assert hits[0]["parameters"] == {"sl_cap": 50}
        assert hits[0]["audit_log_id"] == 7

    def test_search_matches_on_reason_text(self, store):
        store.record_failed_experiment(trigger="t", description="unrelated", reason="drawdown increased")
        hits = store.search_failed_experiments("drawdown")
        assert len(hits) == 1


class TestParameterSets:
    def test_record_and_search_by_strategy_and_symbol(self, store):
        store.record_parameter_set(
            strategy_name="exit_engine_v4", symbol="NIFTY", parameters={"sl": 10, "target": 20},
            performance={"net_pnl": 5000}, is_best=True, notes="best so far",
        )
        hits = store.search_parameter_sets(strategy_name="exit_engine_v4", symbol="NIFTY")
        assert len(hits) == 1
        assert hits[0]["parameters"] == {"sl": 10, "target": 20}
        assert hits[0]["performance"] == {"net_pnl": 5000}

    def test_best_ranked_first_regardless_of_recency(self, store):
        store.record_parameter_set(strategy_name="s", symbol="NIFTY", parameters={"a": 1}, is_best=False)
        store.record_parameter_set(strategy_name="s", symbol="NIFTY", parameters={"a": 2}, is_best=False)
        store.record_parameter_set(strategy_name="s", symbol="NIFTY", parameters={"a": 3}, is_best=True)
        hits = store.search_parameter_sets(strategy_name="s")
        assert hits[0]["parameters"] == {"a": 3}

    def test_no_filters_returns_all(self, store):
        store.record_parameter_set(strategy_name="a", symbol="NIFTY", parameters={})
        store.record_parameter_set(strategy_name="b", symbol="BANKNIFTY", parameters={})
        assert len(store.search_parameter_sets()) == 2


class TestStrategyEvolution:
    def test_record_and_search_by_strategy(self, store):
        store.record_strategy_evolution(
            strategy_name="exit_engine_v4", version_label="v4.1",
            change_summary="tighter stop-loss ratchet", rationale="reduce drawdown", audit_log_id=3,
        )
        hits = store.search_strategy_evolution(strategy_name="exit_engine_v4")
        assert len(hits) == 1
        assert hits[0]["version_label"] == "v4.1"
        assert hits[0]["rationale"] == "reduce drawdown"

    def test_unfiltered_search_returns_all_strategies(self, store):
        store.record_strategy_evolution(strategy_name="a", version_label="v1", change_summary="x")
        store.record_strategy_evolution(strategy_name="b", version_label="v1", change_summary="y")
        assert len(store.search_strategy_evolution()) == 2


class TestBacktestHistory:
    def test_record_and_list_round_trip(self, store):
        store.record_backtest(
            symbol="NIFTY", date_from="2026-05-01", date_to="2026-08-04",
            stats={"net_pnl": 1234.5, "win_rate": 55.0}, comparison={"regressions": []},
            branch="agent/dev-xyz", audit_log_id=9,
        )
        hits = store.list_backtest_history(symbol="NIFTY")
        assert len(hits) == 1
        assert hits[0]["stats"] == {"net_pnl": 1234.5, "win_rate": 55.0}
        assert hits[0]["comparison"] == {"regressions": []}
        assert hits[0]["branch"] == "agent/dev-xyz"

    def test_filters_by_symbol(self, store):
        store.record_backtest(symbol="NIFTY", date_from="a", date_to="b", stats={})
        store.record_backtest(symbol="BANKNIFTY", date_from="a", date_to="b", stats={})
        assert len(store.list_backtest_history(symbol="NIFTY")) == 1


class TestPerformanceHistory:
    def test_record_and_list_round_trip(self, store):
        store.record_performance(symbol="NIFTY", context="backtest", metrics={"sharpe": 1.4}, notes="stable")
        hits = store.list_performance_history(symbol="NIFTY", context="backtest")
        assert len(hits) == 1
        assert hits[0]["metrics"] == {"sharpe": 1.4}
        assert hits[0]["notes"] == "stable"

    def test_filters_by_context(self, store):
        store.record_performance(symbol="NIFTY", context="backtest", metrics={})
        store.record_performance(symbol="NIFTY", context="live", metrics={})
        assert len(store.list_performance_history(symbol="NIFTY", context="live")) == 1
