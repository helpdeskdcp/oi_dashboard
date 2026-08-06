"""
test_agents/trading_supervisor/test_conflict_detector.py -- regression
tests for conflict_detector.py.
"""
from agents.memory.sqlite_store import SQLiteMemoryStore
from agents.trading_supervisor import conflict_detector


class TestActiveStrategyDirections:
    def test_only_is_best_rows_with_a_known_direction_are_included(self, tmp_path):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        store.record_parameter_set(
            strategy_name="a", symbol="NIFTY", parameters={"direction": "long"}, is_best=True,
        )
        store.record_parameter_set(
            strategy_name="b", symbol="NIFTY", parameters={"direction": "unknown"}, is_best=True,
        )
        store.record_parameter_set(
            strategy_name="c", symbol="NIFTY", parameters={}, is_best=True,  # pre-Milestone-7 row, no direction key
        )
        store.record_parameter_set(
            strategy_name="d", symbol="NIFTY", parameters={"direction": "short"}, is_best=False,
        )
        result = conflict_detector.active_strategy_directions(store)
        assert [r["strategy_name"] for r in result] == ["a"]

    def test_excludes_the_named_strategy(self, tmp_path):
        store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
        store.record_parameter_set(
            strategy_name="a", symbol="NIFTY", parameters={"direction": "long"}, is_best=True,
        )
        result = conflict_detector.active_strategy_directions(store, exclude_strategy_name="a")
        assert result == []


class TestDetectConflicts:
    def test_opposite_direction_same_symbol_is_a_conflict(self):
        active = [{"strategy_name": "existing", "symbol": "NIFTY", "direction": "short"}]
        conflicts = conflict_detector.detect_conflicts("candidate", "NIFTY", "long", active)
        assert len(conflicts) == 1
        assert conflicts[0].strategy_b == "existing"

    def test_same_direction_is_not_a_conflict(self):
        active = [{"strategy_name": "existing", "symbol": "NIFTY", "direction": "long"}]
        assert conflict_detector.detect_conflicts("candidate", "NIFTY", "long", active) == []

    def test_different_symbol_is_not_a_conflict(self):
        active = [{"strategy_name": "existing", "symbol": "BANKNIFTY", "direction": "short"}]
        assert conflict_detector.detect_conflicts("candidate", "NIFTY", "long", active) == []

    def test_candidate_direction_both_never_conflicts(self):
        active = [{"strategy_name": "existing", "symbol": "NIFTY", "direction": "short"}]
        assert conflict_detector.detect_conflicts("candidate", "NIFTY", "both", active) == []
