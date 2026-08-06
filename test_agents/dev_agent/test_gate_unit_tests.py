"""
test_agents/dev_agent/test_gate_unit_tests.py -- Gate 1, exercised
against a synthetic pytest project in tmp_path (never this project's own
suite -- that would make the test's own runtime depend on this repo's
403+ tests).
"""
import subprocess

import pytest

from agents.dev_agent.gates import unit_tests
from agents.dev_agent.gates.base import GateStatus


def _write(path, content):
    path.write_text(content)


class TestUnitTestsGate:
    def test_passes_on_a_green_suite(self, tmp_path):
        _write(tmp_path / "test_ok.py", "def test_trivially_true():\n    assert 1 + 1 == 2\n")
        result = unit_tests.run(str(tmp_path))
        assert result.gate == "unit_tests"
        assert result.status == GateStatus.PASSED
        assert result.details["returncode"] == 0

    def test_fails_on_a_red_suite(self, tmp_path):
        _write(tmp_path / "test_broken.py", "def test_deliberately_false():\n    assert 1 == 2\n")
        result = unit_tests.run(str(tmp_path))
        assert result.status == GateStatus.FAILED
        assert result.details["returncode"] != 0

    def test_timeout_is_reported_as_failed(self, tmp_path, monkeypatch):
        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs.get("timeout", 1))

        monkeypatch.setattr(unit_tests.subprocess, "run", fake_run)
        result = unit_tests.run(str(tmp_path), timeout=1)
        assert result.status == GateStatus.FAILED
        assert "timed out" in result.summary
