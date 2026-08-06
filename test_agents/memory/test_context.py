"""
test_agents/memory/test_context.py -- regression tests for
agents/memory/context.py's prompt-formatting logic.
"""
from agents.memory import context
from agents.memory.sqlite_store import SQLiteMemoryStore


def test_returns_placeholder_when_nothing_found(tmp_path):
    store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
    text = context.build_context(store, trigger="brand new trigger", target_files=["nothing.py"])
    assert "No relevant history" in text


def test_includes_bug_fixes_section_when_present(tmp_path):
    store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
    store.record_bug_fix(
        trigger="t", issue_summary="off-by-one bug", root_cause="r", fix_summary="fixed the range bound",
        target_files=["backtest.py"],
    )
    text = context.build_context(store, trigger="off-by-one", target_files=["backtest.py"])
    assert "Past bugs & fixes" in text
    assert "off-by-one bug" in text
    assert "fixed the range bound" in text


def test_includes_failed_experiments_section_when_present(tmp_path):
    store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
    store.record_failed_experiment(
        trigger="t", description="widened SL cap", reason="regressed net P&L", target_files=["exit_engine_v4.py"],
    )
    text = context.build_context(store, trigger="SL cap", target_files=["exit_engine_v4.py"])
    assert "Failed experiments" in text
    assert "regressed net P&L" in text


def test_includes_parameter_sets_only_when_strategy_or_symbol_given(tmp_path):
    store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
    store.record_parameter_set(strategy_name="exit_engine_v4", symbol="NIFTY", parameters={"sl": 10}, is_best=True)

    without_symbol = context.build_context(store, trigger="t", target_files=["x.py"])
    assert "No relevant history" in without_symbol

    with_symbol = context.build_context(store, trigger="t", target_files=["x.py"], symbol="NIFTY")
    assert "Known parameter sets" in with_symbol
    assert "exit_engine_v4/NIFTY" in with_symbol


def test_includes_strategy_evolution_only_when_strategy_name_given(tmp_path):
    store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
    store.record_strategy_evolution(strategy_name="exit_engine_v4", version_label="v4.1", change_summary="tighter SL")

    without_strategy = context.build_context(store, trigger="t", target_files=["x.py"])
    assert "No relevant history" in without_strategy

    with_strategy = context.build_context(store, trigger="t", target_files=["x.py"], strategy_name="exit_engine_v4")
    assert "Strategy evolution history" in with_strategy
    assert "tighter SL" in with_strategy


def test_respects_limit(tmp_path):
    store = SQLiteMemoryStore(db_path=str(tmp_path / "m.db"))
    for i in range(5):
        store.record_bug_fix(trigger="t", issue_summary=f"bug {i}", root_cause="r", fix_summary="f")
    text = context.build_context(store, trigger="bug", target_files=[], limit=2)
    assert text.count("-> fix:") == 2
