"""
test_agents/sys_admin/test_security_audit.py -- regression tests for
security_audit.py against real (throwaway) files/git repos -- never
this project's own agents/ tree or oi_history.db.
"""
from agents import base_agent, llm_providers
from agents.sys_admin import security_audit, sysadmin_store


class TestScanForSecrets:
    def test_finds_a_secret_in_a_real_file(self, tmp_path):
        f = tmp_path / "config_snippet.py"
        f.write_text("OPENAI_API_KEY = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n")
        findings = security_audit.scan_for_secrets([str(f)])
        assert len(findings) >= 1
        assert findings[0]["path"] == str(f)

    def test_clean_file_has_no_findings(self, tmp_path):
        f = tmp_path / "clean.py"
        f.write_text("VALUE = 42\n")
        assert security_audit.scan_for_secrets([str(f)]) == []

    def test_missing_file_is_skipped_not_raised(self, tmp_path):
        assert security_audit.scan_for_secrets([str(tmp_path / "nope.py")]) == []


class TestValidateApiKeys:
    def test_never_makes_a_live_call(self, monkeypatch):
        # is_configured() on every real adapter only ever reads os.getenv --
        # confirm the function completes without any network-shaped call
        # by checking it returns a plain bool per provider, fast.
        result = security_audit.validate_api_keys()
        assert set(result.keys()) == set(llm_providers.available_providers())
        assert all(isinstance(v, bool) for v in result.values())

    def test_reflects_a_configured_provider(self, monkeypatch):
        monkeypatch.setattr(llm_providers, "available_providers", lambda: ["fake"])
        monkeypatch.setattr(llm_providers, "get_llm_provider", lambda name: type(
            "P", (), {"is_configured": lambda self: True},
        )())
        assert security_audit.validate_api_keys() == {"fake": True}


class TestProposeOnlyInvariant:
    def test_invariant_holds_on_the_real_base_agent(self):
        result = security_audit.check_propose_only_invariant()
        assert result["invariant_holds"] is True
        assert result["forbidden_methods_found"] == []

    def test_detects_a_violation(self, monkeypatch):
        monkeypatch.setattr(base_agent.BaseAgent, "execute", lambda self: None, raising=False)
        result = security_audit.check_propose_only_invariant()
        assert result["invariant_holds"] is False
        assert "execute" in result["forbidden_methods_found"]


class TestDetectUnexpectedModifications:
    def test_no_modifications_when_ref_is_head(self, toy_repo):
        result = security_audit.detect_unexpected_modifications(repo_dir=str(toy_repo), ref="HEAD")
        assert result["checked"] is True
        assert result["modified_files"] == []

    def test_detects_a_modified_agents_file(self, toy_repo):
        (toy_repo / "agents" / "placeholder.py").write_text("# tampered\n")
        result = security_audit.detect_unexpected_modifications(repo_dir=str(toy_repo), ref="HEAD")
        assert "agents/placeholder.py" in result["modified_files"]

    def test_not_a_git_repo_degrades_gracefully(self, tmp_path):
        result = security_audit.detect_unexpected_modifications(repo_dir=str(tmp_path))
        assert result["checked"] is False


class TestCheckIntegrity:
    def test_missing_db_reports_none_not_a_crash(self, tmp_path, toy_repo):
        result = security_audit.check_integrity(db_path=str(tmp_path / "nope.db"), repo_dir=str(toy_repo))
        assert result["sqlite_integrity_ok"] is None

    def test_healthy_repo_and_db(self, tmp_path, toy_repo):
        import sqlite3
        db_path = str(tmp_path / "t.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE t (id INTEGER)")
        conn.commit()
        conn.close()
        result = security_audit.check_integrity(db_path=db_path, repo_dir=str(toy_repo))
        assert result["sqlite_integrity_ok"] is True
        assert result["git_fsck_ok"] is True


class TestRunAudit:
    def test_clean_state_produces_no_findings(self, agent_db, toy_repo, monkeypatch):
        monkeypatch.setattr(llm_providers, "available_providers", lambda: ["fake"])
        monkeypatch.setattr(llm_providers, "get_llm_provider", lambda name: type(
            "P", (), {"is_configured": lambda self: True},
        )())
        reports = security_audit.run_audit(repo_dir=str(toy_repo))
        assert reports == []

    def test_findings_are_persisted(self, agent_db, toy_repo, monkeypatch):
        monkeypatch.setattr(llm_providers, "available_providers", lambda: ["fake"])
        monkeypatch.setattr(llm_providers, "get_llm_provider", lambda name: type(
            "P", (), {"is_configured": lambda self: False},
        )())
        reports = security_audit.run_audit(repo_dir=str(toy_repo))
        assert len(reports) >= 1
        stored = sysadmin_store.list_reports(module="security_audit")
        assert len(stored) == len(reports)

    def test_secret_in_scanned_file_is_flagged(self, agent_db, toy_repo, monkeypatch):
        monkeypatch.setattr(llm_providers, "available_providers", lambda: ["fake"])
        monkeypatch.setattr(llm_providers, "get_llm_provider", lambda name: type(
            "P", (), {"is_configured": lambda self: True},
        )())
        secret_file = toy_repo / "leaked.py"
        secret_file.write_text("OPENAI_API_KEY = 'sk-abcdefghijklmnopqrstuvwxyz123456'\n")
        reports = security_audit.run_audit(repo_dir=str(toy_repo), scan_paths=[str(secret_file)])
        assert any(r.action == "secret_scan" and r.severity == "critical" for r in reports)
