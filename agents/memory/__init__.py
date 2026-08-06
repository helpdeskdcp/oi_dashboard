"""
agents/memory/ -- the AI Memory & Knowledge Base. Requirement: "Use
SQLite first, with a clean interface that can later be upgraded to
PostgreSQL." get_memory_store() is the ONLY way any other module touches
storage -- the same pattern agents.llm_providers.get_llm_provider()
already establishes for LLM backends. Nothing outside this package
imports sqlite3 or SQLiteMemoryStore directly, and nothing outside this
module reads agents.config.MEMORY_BACKEND. Adding a PostgresMemoryStore
later is one new adapter module implementing MemoryStore plus one line in
_BACKENDS -- no caller (detector.py, patcher.py, pipeline.py) changes.
"""
from .. import config
from .base import MemoryStore, MemoryStoreError


def _load_sqlite() -> MemoryStore:
    from .sqlite_store import SQLiteMemoryStore

    return SQLiteMemoryStore(db_path=config.MEMORY_DB_PATH)


_BACKENDS = {"sqlite": _load_sqlite}


def available_backends() -> list[str]:
    return sorted(_BACKENDS)


def get_memory_store(backend: str | None = None) -> MemoryStore:
    """backend=None (default) reads agents.config.MEMORY_BACKEND. Raises
    MemoryStoreError for an unknown backend name, so a typo'd env var
    fails loudly at first use rather than silently falling back to
    something unexpected."""
    name = backend or config.MEMORY_BACKEND
    if name not in _BACKENDS:
        raise MemoryStoreError(f"unknown memory backend {name!r} -- available: {available_backends()}")
    return _BACKENDS[name]()
