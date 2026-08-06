"""
test_agents/test_registry.py -- regression tests for agents/registry.py
(the plugin registration mechanism).
"""
import pytest

from agents import registry


@pytest.fixture(autouse=True)
def _clean_registry():
    # The registry is module-level global state, shared across the whole
    # test session -- snapshot/restore around each test (not clear/clear)
    # so this file's throwaway registrations never leak into other test
    # modules, WITHOUT permanently wiping a real agent's own
    # @register_agent registration (e.g. agents.trading_supervisor.
    # supervisor_agent.TradingSupervisor, registered once at import time)
    # for the remainder of the session just because this file happened to
    # run. Restoring the exact prior dict, not merely "some agents,"
    # matters here: a naive clear-then-restore-nothing left the registry
    # permanently empty after this file ran, a real bug this milestone's
    # first production register_agent() call would otherwise have hit.
    original = dict(registry._AGENTS)
    registry._reset_for_tests()
    yield
    registry._AGENTS.clear()
    registry._AGENTS.update(original)


class TestRegisterAndGet:
    def test_register_then_get_returns_the_same_class(self):
        @registry.register_agent("toy")
        class ToyAgent:
            pass

        assert registry.get_agent("toy") is ToyAgent

    def test_get_unknown_name_raises_key_error(self):
        with pytest.raises(KeyError):
            registry.get_agent("does-not-exist")

    def test_duplicate_name_raises_value_error(self):
        @registry.register_agent("dup")
        class First:
            pass

        with pytest.raises(ValueError):
            @registry.register_agent("dup")
            class Second:
                pass

    def test_registered_agents_lists_all_names_sorted(self):
        @registry.register_agent("zeta")
        class Zeta:
            pass

        @registry.register_agent("alpha")
        class Alpha:
            pass

        assert registry.registered_agents() == ["alpha", "zeta"]

    def test_decorator_returns_the_original_class_unchanged(self):
        @registry.register_agent("passthrough")
        class Passthrough:
            marker = 42

        assert Passthrough.marker == 42
