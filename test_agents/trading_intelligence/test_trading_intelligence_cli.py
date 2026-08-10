"""
test_agents/trading_intelligence/test_trading_intelligence_cli.py --
regression tests for trading_intelligence_cli.py, the manual-only
entrypoint approved to run agents.trading_intelligence.api.
run_scheduled_cycle() on demand (Today Signal Audit follow-up,
2026-08-10). Lives in this package's own test subdirectory (not repo
root) to reuse conftest.py's ti_db/insert_realistic_chain fixtures,
matching every other test file for this Milestone 10 package.
"""
import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import trading_intelligence_cli as cli  # noqa: E402
from agents.trading_intelligence import ti_store  # noqa: E402

from .conftest import insert_realistic_chain  # noqa: E402


def _today_ts(hh="10", mm="00", ss="00"):
    return f"{dt.date.today().isoformat()}T{hh}:{mm}:{ss}"


class TestRunCycle:
    def test_prints_one_line_per_symbol_and_returns_zero(self, ti_db, monkeypatch, capsys):
        monkeypatch.setattr(cli.ti_api, "run_scheduled_cycle", lambda **kw: {
            "NIFTY": {"available": True, "action": "BUY CE", "trade_opened": True, "trade_id": 12},
            "BANKNIFTY": {"available": True, "action": "NO_TRADE", "trade_opened": False, "trade_id": None},
        })
        rc = cli._cmd_run_cycle(None)
        out = capsys.readouterr().out
        assert rc == 0
        assert "NIFTY: action=BUY CE trade_opened=True trade_id=12" in out
        assert "BANKNIFTY: action=NO_TRADE trade_opened=False" in out

    def test_unavailable_symbol_reports_reason_not_a_crash(self, ti_db, monkeypatch, capsys):
        monkeypatch.setattr(cli.ti_api, "run_scheduled_cycle", lambda **kw: {
            "SENSEX": {"available": False, "reason": "no option-chain cycle has ever been logged for SENSEX",
                       "action": None, "trade_opened": False},
        })
        rc = cli._cmd_run_cycle(None)
        out = capsys.readouterr().out
        assert rc == 0
        assert "SENSEX: unavailable (no option-chain cycle has ever been logged for SENSEX)" in out

    def test_exception_returns_nonzero_exit_code(self, ti_db, monkeypatch, capsys):
        def _raise(**kw):
            raise RuntimeError("boom")
        monkeypatch.setattr(cli.ti_api, "run_scheduled_cycle", _raise)
        rc = cli._cmd_run_cycle(None)
        assert rc == 1
        assert "boom" in capsys.readouterr().err

    def test_run_cycle_is_the_only_command_that_can_write(self, ti_db, monkeypatch):
        """Static guarantee: _cmd_today/_cmd_audit never call
        run_scheduled_cycle, open_trade, close_trade, or record_signal."""
        import inspect
        for fn in (cli._cmd_today, cli._cmd_audit):
            src = inspect.getsource(fn)
            assert "run_scheduled_cycle" not in src
            assert "open_trade(" not in src
            assert "close_trade(" not in src
            assert "record_signal(" not in src


class TestToday:
    def test_no_trades_today_reports_clearly(self, ti_db, capsys):
        rc = cli._cmd_today(None)
        out = capsys.readouterr().out
        assert rc == 0
        assert "No ti_paper_trades rows with entry_time today" in out

    def test_shows_open_trade_with_unrealized_pnl(self, ti_db, capsys):
        insert_realistic_chain(ti_db, symbol="NIFTY", underlying_ltp=24505.0, atm=24500.0, step=50)
        trade_id = ti_store.open_trade(
            symbol="NIFTY", strike=24500.0, direction="CE", entry_price=90.0,
            target_price=120.0, sl_price=70.0, qty=50, confidence=70,
        )
        conn = __import__("sqlite3").connect(ti_db)
        conn.execute("UPDATE ti_paper_trades SET entry_time=? WHERE id=?", (_today_ts(), trade_id))
        conn.commit()
        conn.close()

        rc = cli._cmd_today(None)
        out = capsys.readouterr().out
        assert rc == 0
        assert f"{trade_id}" in out
        assert "NIFTY" in out
        assert "OPEN" in out
        # seeded strike's ce_ltp default is 100.0 (conftest's insert_strike default) -> (100-90)*50 = +500.00
        assert "+500.00 (unrl)" in out

    def test_shows_closed_trade_with_realized_pnl(self, ti_db, capsys):
        trade_id = ti_store.open_trade(
            symbol="NIFTY", strike=24500.0, direction="CE", entry_price=90.0,
            target_price=120.0, sl_price=70.0, qty=50, confidence=70,
        )
        ti_store.close_trade(trade_id, exit_price=120.0, exit_reason="TARGET HIT")
        conn = __import__("sqlite3").connect(ti_db)
        conn.execute("UPDATE ti_paper_trades SET entry_time=? WHERE id=?", (_today_ts(), trade_id))
        conn.commit()
        conn.close()

        rc = cli._cmd_today(None)
        out = capsys.readouterr().out
        assert rc == 0
        assert "CLOSED" in out
        assert "+1500.00 (real)" in out  # (120-90)*50

    def test_trades_from_other_days_excluded(self, ti_db, capsys):
        trade_id = ti_store.open_trade(
            symbol="NIFTY", strike=24500.0, direction="CE", entry_price=90.0,
            target_price=120.0, sl_price=70.0, qty=50, confidence=70,
        )
        conn = __import__("sqlite3").connect(ti_db)
        conn.execute("UPDATE ti_paper_trades SET entry_time=? WHERE id=?", ("2020-01-01T09:00:00", trade_id))
        conn.commit()
        conn.close()

        rc = cli._cmd_today(None)
        out = capsys.readouterr().out
        assert rc == 0
        assert "No ti_paper_trades rows with entry_time today" in out


class TestAudit:
    def test_empty_day_reports_no_data_found(self, ti_db, capsys):
        rc = cli._cmd_audit(None)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Signals generated today: 0" in out
        assert "NO DATA FOUND" in out  # win rate / net P&L

    def test_counts_signals_and_trades_correctly(self, ti_db, capsys):
        ti_store.record_signal(symbol="NIFTY", action="BUY CE", entry_price=90.0)
        ti_store.record_signal(symbol="BANKNIFTY", action="NO_TRADE")
        ti_store.record_signal(symbol="SENSEX", action="NO_TRADE")

        win_id = ti_store.open_trade(symbol="NIFTY", strike=24500.0, direction="CE", entry_price=90.0,
                                      target_price=120.0, sl_price=70.0, qty=50)
        ti_store.close_trade(win_id, exit_price=120.0, exit_reason="TARGET HIT")
        loss_id = ti_store.open_trade(symbol="BANKNIFTY", strike=52000.0, direction="PE", entry_price=200.0,
                                       target_price=260.0, sl_price=170.0, qty=25)
        ti_store.close_trade(loss_id, exit_price=170.0, exit_reason="STOP LOSS")
        open_id = ti_store.open_trade(symbol="SENSEX", strike=78000.0, direction="CE", entry_price=300.0,
                                       target_price=380.0, sl_price=260.0, qty=10)

        conn = __import__("sqlite3").connect(ti_db)
        for tid in (win_id, loss_id, open_id):
            conn.execute("UPDATE ti_paper_trades SET entry_time=? WHERE id=?", (_today_ts(), tid))
        conn.commit()
        conn.close()

        rc = cli._cmd_audit(None)
        out = capsys.readouterr().out
        assert rc == 0
        assert "Signals generated today: 3 (actionable BUY CE/PE: 1)" in out
        assert "Trades opened today: 3" in out
        assert "Active trades: 1" in out
        assert "Target hits: 1" in out
        assert "Stop-loss hits: 1" in out
        assert "Win rate: 50.0%" in out
        # (120-90)*50=+1500, (170-200)*25=-750 -> net +750.00
        assert "Net P&L: +750.00 pts" in out


class TestSafetyUntouched:
    """AST-based (not substring) checks -- the module's own docstring
    legitimately explains what it does NOT touch by naming these exact
    constants, so a plain substring search would false-positive on that
    prose. These check the parsed code itself: no import of the
    scheduler/scheduling_control modules, and no attribute access on the
    two lock constants anywhere in executable code."""

    @staticmethod
    def _code_tree():
        import ast
        source = Path("trading_intelligence_cli.py").read_text()
        tree = ast.parse(source, filename="trading_intelligence_cli.py")
        # Drop the module-level docstring (first Expr/Constant statement)
        # so prose explaining the safety boundary doesn't trip these checks.
        if (tree.body and isinstance(tree.body[0], ast.Expr)
                and isinstance(tree.body[0].value, ast.Constant) and isinstance(tree.body[0].value.value, str)):
            tree.body = tree.body[1:]
        return tree

    def test_cli_never_imports_scheduling_control_or_agent_runtime(self):
        import ast
        tree = self._code_tree()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any("scheduling_control" in a.name or "agent_runtime" in a.name for a in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                assert "scheduling_control" not in node.module
                assert "agent_runtime" not in node.module

    def test_cli_never_touches_the_two_lock_constants_in_code(self):
        import ast
        tree = self._code_tree()
        names_used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attrs_used = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        assert "RUNTIME_SCHEDULER_ENABLED" not in names_used | attrs_used
        assert "NEVER_SCHEDULABLE_AGENTS" not in names_used | attrs_used

    def test_runtime_scheduler_enabled_still_false(self):
        from agents import config as agents_config
        assert agents_config.RUNTIME_SCHEDULER_ENABLED is False

    def test_trading_intelligence_now_schedulable(self):
        """Milestone 17: trading_intelligence was deliberately removed
        from NEVER_SCHEDULABLE_AGENTS -- quant_researcher and
        shadow_mode remain permanently blocked."""
        from agents.runtime import scheduling_control as sc
        assert sc.is_schedulable("trading_intelligence") is True
        assert sc.is_schedulable("quant_researcher") is False
        assert sc.is_schedulable("shadow_mode") is False
