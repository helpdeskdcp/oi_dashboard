import textwrap

from agents.runtime import communication_contract as cc


class TestFindPrivateCrossModuleAccess:
    def test_the_real_runtime_package_has_zero_violations(self):
        assert cc.find_private_cross_module_access() == []

    def test_detects_a_real_violation_in_a_synthetic_package(self, tmp_path):
        (tmp_path / "runtime_store.py").write_text("def _private():\n    pass\n")
        (tmp_path / "workflow_engine.py").write_text(
            textwrap.dedent("""
                from . import runtime_store

                def bad():
                    return runtime_store._private()
            """)
        )
        violations = cc.find_private_cross_module_access(runtime_dir=str(tmp_path))
        assert len(violations) == 1
        assert violations[0]["accessed"] == "runtime_store._private"

    def test_a_modules_own_private_helper_called_by_bare_name_is_not_a_violation(self, tmp_path):
        (tmp_path / "workflow_engine.py").write_text(
            textwrap.dedent("""
                def _helper():
                    return 1

                def public():
                    return _helper()
            """)
        )
        assert cc.find_private_cross_module_access(runtime_dir=str(tmp_path)) == []

    def test_dunder_methods_are_never_flagged(self, tmp_path):
        (tmp_path / "runtime_store.py").write_text("class X:\n    pass\n")
        (tmp_path / "workflow_engine.py").write_text(
            textwrap.dedent("""
                from . import runtime_store

                def f():
                    return runtime_store.__name__
            """)
        )
        assert cc.find_private_cross_module_access(runtime_dir=str(tmp_path)) == []

    def test_accessing_a_non_runtime_modules_private_attribute_is_not_flagged(self, tmp_path):
        """The contract is scoped to agents/runtime/'s OWN modules only
        -- e.g. reaching into agents.sys_admin's internals is a separate
        concern this check doesn't claim to cover."""
        (tmp_path / "agent_runtime.py").write_text(
            textwrap.dedent("""
                from .. import config

                def f():
                    return config._something_not_runtime_owned
            """)
        )
        assert cc.find_private_cross_module_access(runtime_dir=str(tmp_path)) == []
