"""
test_agents/dev_agent/test_gate_integration_tests.py -- Gate 2, exercised
against a synthetic "app.py" + route-test file in tmp_path so the test
doesn't depend on this project's real Flask app.
"""
import subprocess

from agents.dev_agent.gates import integration_tests
from agents.dev_agent.gates.base import GateStatus


class TestIntegrationTestsGate:
    def test_passes_when_app_imports_and_route_tests_pass(self, tmp_path):
        (tmp_path / "app.py").write_text("VALUE = 42\n")
        (tmp_path / "test_backtest_profiles.py").write_text(
            "import app\n\ndef test_value():\n    assert app.VALUE == 42\n"
        )
        result = integration_tests.run(str(tmp_path))
        assert result.status == GateStatus.PASSED

    def test_fails_when_app_does_not_import(self, tmp_path):
        (tmp_path / "app.py").write_text("raise RuntimeError('boom')\n")
        (tmp_path / "test_backtest_profiles.py").write_text("def test_noop():\n    assert True\n")
        result = integration_tests.run(str(tmp_path))
        assert result.status == GateStatus.FAILED
        assert result.details["stage"] == "boot"

    def test_fails_when_route_tests_fail_despite_clean_boot(self, tmp_path):
        (tmp_path / "app.py").write_text("VALUE = 1\n")
        (tmp_path / "test_backtest_profiles.py").write_text(
            "import app\n\ndef test_value():\n    assert app.VALUE == 999\n"
        )
        result = integration_tests.run(str(tmp_path))
        assert result.status == GateStatus.FAILED
        assert result.details["stage"] == "route_tests"

    def test_boot_timeout_is_reported_as_failed(self, tmp_path, monkeypatch):
        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 1))

        monkeypatch.setattr(integration_tests.subprocess, "run", fake_run)
        result = integration_tests.run(str(tmp_path), timeout=1)
        assert result.status == GateStatus.FAILED
        assert result.details["stage"] == "boot"
