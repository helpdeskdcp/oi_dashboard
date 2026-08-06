"""
test_agents/dev_agent/test_gate_code_quality.py -- Gate 5. Mocked at the
shutil.which / subprocess.run boundary (never invokes the real
ruff/mypy/bandit/pip-audit against this repo or its ambient site-packages
-- pip-audit with no target scans the whole active environment, which
would make test outcomes depend on unrelated CVEs) so results are
deterministic regardless of what's installed on the machine running the
suite.
"""
from agents.dev_agent.gates import code_quality
from agents.dev_agent.gates.base import GateStatus


def _fake_which_all_present(name):
    return f"/usr/bin/{name}"


def _fake_which_none_present(name):
    return None


class _FakeCompleted:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestCodeQualityGate:
    def test_skipped_when_no_tools_installed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(code_quality.shutil, "which", _fake_which_none_present)
        result = code_quality.run(str(tmp_path))
        assert result.status == GateStatus.SKIPPED
        assert all(c["status"] == "skipped" for c in result.details["checks"])

    def test_passed_when_all_installed_tools_are_clean(self, tmp_path, monkeypatch):
        monkeypatch.setattr(code_quality.shutil, "which", _fake_which_all_present)
        monkeypatch.setattr(code_quality.subprocess, "run", lambda *a, **k: _FakeCompleted(0))
        result = code_quality.run(str(tmp_path))
        assert result.status == GateStatus.PASSED
        assert all(c["status"] == "passed" for c in result.details["checks"])

    def test_failed_when_any_installed_tool_reports_an_issue(self, tmp_path, monkeypatch):
        monkeypatch.setattr(code_quality.shutil, "which", _fake_which_all_present)

        def fake_run(cmd, **kwargs):
            if cmd[0] == "bandit":
                return _FakeCompleted(1, stdout="B101: assert used\n")
            return _FakeCompleted(0)

        monkeypatch.setattr(code_quality.subprocess, "run", fake_run)
        result = code_quality.run(str(tmp_path))
        assert result.status == GateStatus.FAILED
        assert "security_scan" in result.summary

    def test_passed_with_note_when_some_tools_missing_others_clean(self, tmp_path, monkeypatch):
        def partial_which(name):
            return "/usr/bin/ruff" if name == "ruff" else None

        monkeypatch.setattr(code_quality.shutil, "which", partial_which)
        monkeypatch.setattr(code_quality.subprocess, "run", lambda *a, **k: _FakeCompleted(0))
        result = code_quality.run(str(tmp_path))
        assert result.status == GateStatus.PASSED
        assert "skipped" in result.summary
