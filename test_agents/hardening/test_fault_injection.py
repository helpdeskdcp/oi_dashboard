"""
Production Hardening Sprint -- "Fault injection testing (API down, DB
failure, network loss)."

Two real bugs were found and fixed by the tests in this file (see
PRODUCTION_HARDENING_SPRINT.md for the full writeup):

1. agents.risk_manager.api.get_portfolio_snapshot() let a missing/
   corrupted-schema oi_history.db raise sqlite3.OperationalError
   straight through the /api/risk/portfolio Flask route. Fixed to
   degrade to risk_report.unavailable(...).
2. agents.sys_admin.api.get_overview() -- the Operations Dashboard
   itself -- ran every section as one unbroken chain, so a single
   missing table took down the ENTIRE dashboard, including sections
   that were still perfectly readable. Fixed so each section degrades
   independently via a new _section() helper.

"API down": this framework has never made a live broker API call from
any agent (a hard, structural, documented invariant -- see
agents/risk_manager/data_access.py's and agents/trading_supervisor/
data_health.py's own module docstrings, and this project's own
memory note about a /live-positions test triggering a real duplicate
login). So "the live broker API is down" cannot propagate into agent
logic at all by construction; what CAN happen is oi_history.db (which
DOES receive live data from that API) going stale or unreachable --
that's DB failure, covered below and is the honest way to fault-inject
"API down" without ever making the very live call this framework
refuses to make.
"""
import os
import socket
import sqlite3

from agents.risk_manager import api as risk_api
from agents.sys_admin import api as sysadmin_api
from agents.sys_admin import infra_monitor, self_healing
from test_agents.risk_manager.conftest import insert_user


class TestDatabaseFailure:
    def test_risk_portfolio_snapshot_degrades_honestly_on_missing_tables(self, tmp_path, monkeypatch):
        """A DB file exists but paper_orders/users/etc. (app.py-owned
        tables) were never created -- a real "database not ready yet"
        shape, not a contrived one. Must return a structured, honest
        "unavailable" report, never raise."""
        from agents.risk_manager import data_access
        empty_db = str(tmp_path / "empty.db")
        sqlite3.connect(empty_db).close()
        monkeypatch.setattr(data_access, "DB_PATH", empty_db)

        result = risk_api.get_portfolio_snapshot(user_id=1, persist=False)
        assert result["report_type"] == "portfolio_snapshot"
        assert result["details"]["available"] is False
        assert "no such table" in result["details"]["reason"]

    def test_risk_portfolio_snapshot_works_normally_when_db_is_healthy(self, paper_db):
        """Guards against the fix being a blanket try/except that masks
        real success too -- must still work when the DB genuinely is
        fine (paper_db: the real-shaped schema test_agents/risk_manager/
        already builds), just empty of positions."""
        insert_user(paper_db, 1, wallet_balance=100000.0)
        result = risk_api.get_portfolio_snapshot(user_id=1, persist=False)
        assert result["details"].get("available", True) is True
        assert result["report_type"] == "portfolio_snapshot"

    def test_sysadmin_overview_degrades_per_section_on_missing_tables(self, tmp_path, monkeypatch):
        """The Operations Dashboard is the view meant to show system
        health DURING an incident -- it must not itself go blank because
        of the incident. A fresh, un-initialized DB (every section's
        table missing) must yield a structured per-section error, with
        every OTHER top-level key still present."""
        from agents.memory.sqlite_store import SQLiteMemoryStore
        from agents.risk_manager import risk_store
        from agents.sys_admin import sysadmin_store
        from agents.trading_supervisor import supervision_store
        from agents.runtime import runtime_store

        empty_db = str(tmp_path / "uninitialized.db")
        sqlite3.connect(empty_db).close()
        monkeypatch.setattr(sysadmin_store, "DB_PATH", empty_db)
        monkeypatch.setattr(risk_store, "DB_PATH", empty_db)
        monkeypatch.setattr(supervision_store, "DB_PATH", empty_db)
        monkeypatch.setattr(runtime_store, "DB_PATH", empty_db)
        mstore = SQLiteMemoryStore(db_path=str(tmp_path / "memory.db"))

        overview = sysadmin_api.get_overview(memory_store=mstore, db_path=empty_db)

        expected_keys = {
            "agents", "infrastructure", "risk_state", "supervision_state",
            "backup_state", "security_alerts", "recovery_history", "recent_findings",
            "runtime",  # Milestone 9: the Runtime Dashboard, folded into this same overview
        }
        assert set(overview.keys()) == expected_keys
        for key in ("agents", "risk_state", "supervision_state", "backup_state", "security_alerts", "runtime"):
            assert "error" in overview[key], f"{key} should have degraded honestly, got {overview[key]}"

    def test_sysadmin_overview_one_broken_section_does_not_hide_healthy_ones(self, agent_db, tmp_path):
        """Sysadmin/risk/supervision tables ARE healthy (agent_db
        fixture); the memory store's OWN db is separate -- confirms a
        healthy section renders normally alongside the rest."""
        from agents.memory.sqlite_store import SQLiteMemoryStore
        mstore = SQLiteMemoryStore(db_path=str(tmp_path / "memory.db"))
        overview = sysadmin_api.get_overview(memory_store=mstore, db_path=agent_db)
        assert "error" not in overview["agents"]
        assert "error" not in overview["backup_state"]

    def test_self_healing_detects_a_database_that_does_not_exist(self, agent_db, tmp_path):
        """agent_db gives sysadmin_store (self_healing's OWN bookkeeping
        store) real tables; the database being investigated for recovery
        is a SEPARATE path that genuinely doesn't exist."""
        missing = str(tmp_path / "does_not_exist.db")
        assert not os.path.exists(missing)
        report = self_healing.propose_database_recovery(db_path=missing)
        assert report.severity == "critical"

    def test_infra_monitor_sqlite_status_never_raises_on_a_locked_or_missing_db(self, tmp_path):
        missing = str(tmp_path / "gone.db")
        status = infra_monitor.sqlite_status(db_path=missing)
        assert status["exists"] is False


class TestNetworkLoss:
    def test_network_status_reports_unreachable_without_raising(self, monkeypatch):
        def _always_fails(*args, **kwargs):
            raise OSError("simulated network loss")
        monkeypatch.setattr(socket, "create_connection", _always_fails)
        result = infra_monitor.network_status()
        assert all(host["reachable"] is False for host in result.values())
        assert all("simulated network loss" in host["error"] for host in result.values())

    def test_network_status_handles_a_mix_of_reachable_and_unreachable_hosts(self, monkeypatch):
        def _fail_one(addr, timeout=2.0):
            host, _port = addr
            if host == "10.255.255.1":
                raise OSError("simulated: host unreachable")
            class _Ctx:
                def __enter__(self_inner):
                    return self_inner
                def __exit__(self_inner, *exc):
                    return False
            return _Ctx()
        monkeypatch.setattr(socket, "create_connection", _fail_one)
        result = infra_monitor.network_status(hosts=(("10.255.255.1", 53), ("8.8.8.8", 53)))
        assert result["10.255.255.1"]["reachable"] is False
        assert result["8.8.8.8"]["reachable"] is True


class TestApiDown:
    def test_no_agent_module_ever_touches_the_live_broker_session(self):
        """This project's own established landmine: hitting /live-positions
        in a test has already triggered a real duplicate Angel One login.
        The structural guarantee that makes "the broker API is down" a
        non-event for every agent is that none of them ever call it.
        Verified here by scanning agents/ source for the actual live-SDK
        invocation shapes (never matched inside a docstring/comment,
        where several modules deliberately document NOT calling these --
        see e.g. agents/risk_manager/data_access.py's own module
        docstring -- which would otherwise make this check self-defeating)."""
        import ast

        forbidden = ("SmartConnect", "smartApi")
        agents_dir = os.path.join(os.path.dirname(__file__), "..", "..", "agents")
        violations = []
        for root, _dirs, files in os.walk(agents_dir):
            for fname in files:
                if not fname.endswith(".py"):
                    continue
                path = os.path.join(root, fname)
                with open(path, "r", errors="replace") as fh:
                    source = fh.read()
                tree = ast.parse(source, filename=path)
                for node in ast.walk(tree):
                    name = None
                    if isinstance(node, ast.Name):
                        name = node.id
                    elif isinstance(node, ast.Attribute):
                        name = node.attr
                    if name in forbidden:
                        violations.append((path, name, node.lineno))
        assert violations == [], f"agent module(s) reference the live broker SDK: {violations}"
