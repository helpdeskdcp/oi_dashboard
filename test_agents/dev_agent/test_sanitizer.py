"""
test_agents/dev_agent/test_sanitizer.py -- regression tests for
agents/dev_agent/sanitizer.py's secret/credential/PII redaction.
"""
from agents.dev_agent import sanitizer


class TestSanitize:
    def test_redacts_openai_key(self):
        out = sanitizer.sanitize("use sk-abcdefghijklmnopqrstuvwxyz123456 for auth")
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out
        assert sanitizer.REDACTED in out

    def test_redacts_anthropic_key(self):
        out = sanitizer.sanitize("ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
        assert "sk-ant-api03" not in out

    def test_redacts_aws_access_key(self):
        out = sanitizer.sanitize("aws_access_key_id = AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_redacts_github_token(self):
        out = sanitizer.sanitize("token: ghp_abcdefghijklmnopqrstuvwxyz0123456789")
        assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in out

    def test_redacts_slack_token(self):
        out = sanitizer.sanitize("SLACK_TOKEN=xoxb-1234567890-abcdefghijklmnop")
        assert "xoxb-1234567890" not in out

    def test_redacts_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        out = sanitizer.sanitize(f"Authorization header: {jwt}")
        assert jwt not in out

    def test_redacts_bearer_token(self):
        out = sanitizer.sanitize("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123456789")
        assert "abcdefghijklmnopqrstuvwxyz0123456789" not in out

    def test_redacts_generic_password_assignment(self):
        out = sanitizer.sanitize('DB_PASSWORD = "hunter2superSecret"')
        assert "hunter2superSecret" not in out

    def test_redacts_generic_secret_env_style(self):
        out = sanitizer.sanitize("ANGEL_ONE_CLIENT_SECRET=abcd1234efgh5678")
        assert "abcd1234efgh5678" not in out

    def test_redacts_email(self):
        out = sanitizer.sanitize("contact helpdeskdcp@gmail.com for access")
        assert "helpdeskdcp@gmail.com" not in out

    def test_redacts_phone_number_with_dashes(self):
        out = sanitizer.sanitize("call me at +91-98765-43210 tomorrow")
        assert "98765-43210" not in out

    def test_leaves_ordinary_python_code_untouched(self):
        code = (
            "def compute_advanced_trade_stats(trades):\n"
            "    net_pnl = sum(t['points'] for t in trades)\n"
            "    max_drawdown = 1234.5678\n"
            "    strike = 24500\n"
            "    return {'net_pnl': net_pnl, 'max_drawdown': max_drawdown}\n"
        )
        assert sanitizer.sanitize(code) == code

    def test_leaves_decimal_numeric_literals_untouched(self):
        # Decimal points are not phone-number separators -- a strategy
        # file full of price/points literals must not be mangled.
        code = "SL_CAP = 45.6789\nTARGET_RATCHET = 12.3456\nPOINTS = 100.500\n"
        assert sanitizer.sanitize(code) == code

    def test_empty_and_none_safe(self):
        assert sanitizer.sanitize("") == ""
        assert sanitizer.sanitize(None) is None


class TestSanitizeFiles:
    def test_sanitizes_every_value_keeps_keys(self):
        files = {
            "a.py": "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz123456",
            "b.py": "VALUE = 42\n",
        }
        out = sanitizer.sanitize_files(files)
        assert set(out.keys()) == {"a.py", "b.py"}
        assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in out["a.py"]
        assert out["b.py"] == "VALUE = 42\n"
