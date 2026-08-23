"""
test_agents/dev_agent/test_gate_interpreter.py -- regression test for the
"gate ran against the wrong Python" bug.

Every gate that shells out to pytest/python used to hardcode "python3".
Production runs from venv/bin/python3 (run_forever_vps.sh), whose site-
packages are the only ones with this app's dependencies -- so the system
python3 those gates invoked could not even `import app`, and Gate 1/2/3
failed for a reason unrelated to the diff being gated. Every such call
site must use sys.executable.
"""
import subprocess
import sys

from agents.dev_agent.gates import backtest_compare, integration_tests, unit_tests
from agents.sys_admin import maintenance


class _Recorder:
    """Stands in for subprocess.run, capturing every argv it is handed."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.calls = []
        self._result = subprocess.CompletedProcess([], returncode, stdout, stderr)

    def __call__(self, argv, **kwargs):
        self.calls.append(argv)
        return self._result

    @property
    def interpreters(self):
        return [argv[0] for argv in self.calls]


class TestGatesUseTheRunningInterpreter:
    def test_unit_tests_gate(self, monkeypatch, tmp_path):
        recorder = _Recorder()
        monkeypatch.setattr(subprocess, "run", recorder)
        unit_tests.run(str(tmp_path))
        assert recorder.interpreters == [sys.executable]

    def test_integration_tests_gate_boot_check_and_pytest(self, monkeypatch, tmp_path):
        recorder = _Recorder()
        monkeypatch.setattr(subprocess, "run", recorder)
        integration_tests.run(str(tmp_path))
        assert recorder.interpreters == [sys.executable, sys.executable]
        assert recorder.calls[0][1:] == ["-c", "import app"]

    def test_backtest_compare_scenario(self, monkeypatch, tmp_path):
        recorder = _Recorder(stdout='{"stats": {}, "points": [], "cycle_count": 0}\n')
        monkeypatch.setattr(subprocess, "run", recorder)
        backtest_compare._run_scenario(str(tmp_path), "NIFTY", "2026-07-01", "2026-07-02", 60)
        assert recorder.interpreters == [sys.executable]

    def test_sysadmin_test_timing(self, monkeypatch, tmp_path):
        recorder = _Recorder()
        monkeypatch.setattr(subprocess, "run", recorder)
        maintenance.run_test_timing(repo_dir=str(tmp_path))
        assert recorder.interpreters == [sys.executable]


class TestNoHardcodedPython3Remains:
    """Source-level guard against the pattern coming back in a new gate."""

    def test_gate_modules_do_not_hardcode_python3(self):
        offenders = []
        for module in (unit_tests, integration_tests, backtest_compare, maintenance):
            source = open(module.__file__).read()
            for lineno, line in enumerate(source.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue   # the explanatory comments naturally mention "python3"
                if '"python3"' in stripped or "'python3'" in stripped:
                    offenders.append(f"{module.__name__}:{lineno}: {stripped}")
        assert offenders == []
