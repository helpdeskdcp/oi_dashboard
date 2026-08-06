"""
agents/memory/base.py -- the one interface every memory backend must
implement. Requirement: "Store bugs and fixes, backtest history,
performance history, failed experiments with reasons, best parameter
sets, strategy evolution... every AI proposal must search this memory
before generating code... SQLite first, with a clean interface that can
later be upgraded to PostgreSQL." agents/dev_agent/detector.py and
patcher.py only ever call agents.memory.get_memory_store() and talk to
the MemoryStore they get back -- they never import sqlite3 or know which
backend is actually running.
"""
import abc


class MemoryStoreError(Exception):
    """Raised by every backend for every kind of failure -- callers catch
    this ONE exception type regardless of which concrete backend is
    active, the same pattern agents.llm_providers.LLMProviderError
    already establishes."""


class MemoryStore(abc.ABC):
    backend_name: str = "base"

    @abc.abstractmethod
    def init_db(self) -> None:
        """Idempotent (CREATE TABLE IF NOT EXISTS or equivalent) -- safe
        to call on every process start, matching agents.audit_log's own
        init_db() convention. Every write/search method below may assume
        this has already run."""
        raise NotImplementedError

    # --- writes: one method per requirement category ----------------------

    @abc.abstractmethod
    def record_bug_fix(self, *, trigger: str, issue_summary: str, root_cause: str,
                        fix_summary: str, target_files: list | None = None,
                        confidence_score: int | None = None, audit_log_id: int | None = None,
                        outcome: str | None = None) -> int:
        raise NotImplementedError

    @abc.abstractmethod
    def record_backtest(self, *, symbol: str, date_from: str, date_to: str, stats: dict,
                         comparison: dict | None = None, branch: str | None = None,
                         audit_log_id: int | None = None) -> int:
        raise NotImplementedError

    @abc.abstractmethod
    def record_performance(self, *, symbol: str, context: str, metrics: dict,
                            notes: str | None = None) -> int:
        raise NotImplementedError

    @abc.abstractmethod
    def record_failed_experiment(self, *, trigger: str, description: str, reason: str,
                                  target_files: list | None = None, parameters: dict | None = None,
                                  audit_log_id: int | None = None) -> int:
        raise NotImplementedError

    @abc.abstractmethod
    def record_parameter_set(self, *, strategy_name: str, symbol: str, parameters: dict,
                              performance: dict | None = None, is_best: bool = False,
                              notes: str | None = None) -> int:
        raise NotImplementedError

    @abc.abstractmethod
    def record_strategy_evolution(self, *, strategy_name: str, version_label: str,
                                   change_summary: str, rationale: str | None = None,
                                   audit_log_id: int | None = None) -> int:
        raise NotImplementedError

    # --- reads / search -----------------------------------------------------

    @abc.abstractmethod
    def search_bug_fixes(self, query: str, *, target_files: list | None = None, limit: int = 10) -> list:
        raise NotImplementedError

    @abc.abstractmethod
    def search_failed_experiments(self, query: str, *, target_files: list | None = None, limit: int = 10) -> list:
        raise NotImplementedError

    @abc.abstractmethod
    def search_parameter_sets(self, *, strategy_name: str | None = None, symbol: str | None = None,
                               limit: int = 10) -> list:
        raise NotImplementedError

    @abc.abstractmethod
    def search_strategy_evolution(self, *, strategy_name: str | None = None, limit: int = 10) -> list:
        raise NotImplementedError

    @abc.abstractmethod
    def list_backtest_history(self, *, symbol: str | None = None, limit: int = 10) -> list:
        raise NotImplementedError

    @abc.abstractmethod
    def list_performance_history(self, *, symbol: str | None = None, context: str | None = None,
                                  limit: int = 10) -> list:
        raise NotImplementedError
