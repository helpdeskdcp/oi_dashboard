"""
test_agents/memory/test_memory_init.py -- regression tests for
agents/memory/__init__.py's backend selector, matching the pattern
test_llm_providers.py already uses for agents.llm_providers.get_llm_provider().
"""
import pytest

from agents import config
from agents.memory import MemoryStoreError, available_backends, get_memory_store
from agents.memory.sqlite_store import SQLiteMemoryStore


class TestAvailableBackends:
    def test_lists_sqlite(self):
        assert available_backends() == ["sqlite"]


class TestGetMemoryStore:
    def test_unknown_backend_raises(self):
        with pytest.raises(MemoryStoreError, match="unknown memory backend"):
            get_memory_store("postgres")

    def test_default_backend_reads_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "MEMORY_BACKEND", "sqlite")
        monkeypatch.setattr(config, "MEMORY_DB_PATH", str(tmp_path / "m.db"))
        store = get_memory_store()
        assert isinstance(store, SQLiteMemoryStore)

    def test_explicit_backend_overrides_config(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "MEMORY_BACKEND", "sqlite")
        monkeypatch.setattr(config, "MEMORY_DB_PATH", str(tmp_path / "m.db"))
        store = get_memory_store("sqlite")
        assert isinstance(store, SQLiteMemoryStore)

    def test_store_uses_configured_db_path(self, monkeypatch, tmp_path):
        db_path = str(tmp_path / "custom.db")
        monkeypatch.setattr(config, "MEMORY_DB_PATH", db_path)
        store = get_memory_store("sqlite")
        assert store.db_path == db_path
