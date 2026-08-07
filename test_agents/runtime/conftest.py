"""
test_agents/runtime/ -- Milestone 9 (AI Autonomous Runtime &
Orchestration Engine) test suite. Reuses agent_db (test_agents/
conftest.py, now also initializes agents.runtime.runtime_store's
tables), paper_db (test_agents/risk_manager/conftest.py, a real-shaped
schema for risk_manager's data_access reads), and toy_repo/git/
commit_on_new_branch (test_agents/dev_agent/conftest.py, for
approval_engine's real git-merge tests) -- same re-export pattern every
prior milestone's own test conftest already established.
"""
import pytest

from agents.memory.sqlite_store import SQLiteMemoryStore
from agents.risk_manager import data_access as risk_data_access
from test_agents.dev_agent.conftest import (  # noqa: F401
    commit_on_new_branch,
    git,
    toy_repo,
)
from test_agents.risk_manager.conftest import paper_db  # noqa: F401


@pytest.fixture()
def memory_store(tmp_path):
    return SQLiteMemoryStore(db_path=str(tmp_path / "memory.db"))


@pytest.fixture()
def risk_data_access_db(agent_db, monkeypatch):
    """Points agents.risk_manager.data_access at the SAME throwaway DB
    agent_db already built (which does NOT include the paper_orders/
    users schema -- that's what makes this fixture useful for tests
    that want risk_manager's OWN cycle to genuinely fail, e.g.
    agent_runtime.py's failure/escalation-path tests)."""
    monkeypatch.setattr(risk_data_access, "DB_PATH", agent_db)
    return agent_db
