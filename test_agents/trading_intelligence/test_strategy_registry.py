"""
test_agents/trading_intelligence/test_strategy_registry.py -- regression
tests for strategy_registry.get_registry(). Pure unit tests, no DB, no
Flask -- see test_strategy_registry_route.py at repo root for the
GET /api/strategy-registry route's own auth/contract tests.
"""
from agents.trading_intelligence import strategy_registry
from agents import config


class TestRegistryShape:
    def test_every_entry_has_required_fields(self):
        for entry in strategy_registry.get_registry():
            assert set(entry.keys()) == {"flag", "module", "description", "enabled"}
            assert entry["flag"].startswith("TI_ENABLE_")
            assert isinstance(entry["module"], str) and entry["module"]
            assert isinstance(entry["description"], str) and entry["description"]
            assert isinstance(entry["enabled"], bool)

    def test_every_flag_actually_exists_in_agents_config(self):
        """Guards against the registry silently drifting from reality --
        a flag listed here that no longer exists in agents/config.py would
        otherwise report enabled=False for a nonexistent flag rather than
        erroring, which is a much worse failure mode (invisible)."""
        for entry in strategy_registry.get_registry():
            assert hasattr(config, entry["flag"]), f"{entry['flag']} not found in agents.config"

    def test_no_duplicate_flags(self):
        flags = [e["flag"] for e in strategy_registry.get_registry()]
        assert len(flags) == len(set(flags))


class TestLiveSnapshot:
    def test_enabled_value_reflects_current_config_not_a_cached_copy(self, monkeypatch):
        monkeypatch.setattr(config, "TI_ENABLE_STRUCTURE_ALERTS", True)
        entry = next(e for e in strategy_registry.get_registry() if e["flag"] == "TI_ENABLE_STRUCTURE_ALERTS")
        assert entry["enabled"] is True

        monkeypatch.setattr(config, "TI_ENABLE_STRUCTURE_ALERTS", False)
        entry = next(e for e in strategy_registry.get_registry() if e["flag"] == "TI_ENABLE_STRUCTURE_ALERTS")
        assert entry["enabled"] is False

    def test_never_mutates_config(self):
        """Read-only contract -- calling get_registry() must never write
        back to agents.config."""
        before = config.TI_ENABLE_MOMENTUM_CONFIRMATION
        strategy_registry.get_registry()
        assert config.TI_ENABLE_MOMENTUM_CONFIRMATION == before
