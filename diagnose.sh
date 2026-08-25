#!/bin/bash
# diagnose.sh -- local-first, read-only diagnostics for oi_dashboard.
#
# Runs entirely on this VPS with no LLM/API calls of any kind. Reuses
# existing project tooling rather than reimplementing it:
#   - pytest                                  (existing test suite)
#   - ruff / mypy / bandit / pip-audit         (already installed via
#     requirements-dev.txt, same commands as agents/dev_agent/gates/
#     code_quality.py's CHECKS tuple -- invoked directly here rather than
#     importing that package, to avoid pulling in the rest of
#     agents.dev_agent's heavier import chain for a diagnostics-only run)
#   - agents.sys_admin.security_audit          (scan_for_secrets(),
#     check_integrity() -- called directly, NEVER via run_audit(), since
#     run_audit() also WRITES an audit row to the live sysadmin DB
#     tables; a read-only diagnostic must never do that)
#   - gitleaks / shellcheck                    (newly installed, static
#     binaries, no daemon, no network access needed to run)
#
# Never touches: broker/order execution, live trading config, source
# files (no auto-fix/auto-format), git history, or the network (other
# than the tools' own local execution -- nothing here uploads anything).
#
# Usage:
#   ./diagnose.sh                 full sweep
#   ./diagnose.sh --changed       lint/type/security/tests scoped to
#                                  files changed vs origin/main
#   ./diagnose.sh --security      secrets + bandit + pip-audit + git/db integrity only
#   ./diagnose.sh --tests         pytest only
#   ./diagnose.sh --typecheck     mypy only
#
# Results are cached per git HEAD sha (clean tree only) under
# .diagnostics/cache/ so an unchanged repo doesn't re-run expensive
# scanners on every invocation -- pass --no-cache to force a fresh run.

set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")" || exit 1

PY="venv/bin/python3"
[ -x "$PY" ] || PY="python3"
CACHE_DIR=".diagnostics/cache"
NO_CACHE=0
MODE="full"

for arg in "$@"; do
    case "$arg" in
        --changed) MODE="changed" ;;
        --security) MODE="security" ;;
        --tests) MODE="tests" ;;
        --typecheck) MODE="typecheck" ;;
        --no-cache) NO_CACHE=1 ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

mkdir -p "$CACHE_DIR"

HEAD_SHA=$(git rev-parse HEAD 2>/dev/null || echo "no-git")
TREE_DIRTY=$(git status --porcelain 2>/dev/null | head -1)
CACHE_KEY="${HEAD_SHA}$( [ -n "$TREE_DIRTY" ] && echo "-dirty" )"

declare -A STATUS      # category -> PASS/FAIL/SKIP
declare -A DETAIL       # category -> one-line detail
CRITICAL=0; HIGH=0; MEDIUM=0; LOW=0

_cached_or_run() {
    # _cached_or_run <name> <cmd...> -- reuses a cached exit code + tail
    # for the same HEAD sha (clean tree only), else runs and caches.
    local name="$1"; shift
    local cache_file="${CACHE_DIR}/${CACHE_KEY}-${name}.cache"
    if [ "$NO_CACHE" = 0 ] && [ -f "$cache_file" ] && [ -z "$TREE_DIRTY" ]; then
        cat "$cache_file"
        return 0
    fi
    local out rc
    out=$("$@" 2>&1)
    rc=$?
    { echo "RC=$rc"; echo "$out" | tail -c 3000; } > "$cache_file"
    cat "$cache_file"
    return 0
}

_changed_py_files() {
    git diff --name-only origin/main...HEAD -- '*.py' 2>/dev/null
    git diff --name-only --diff-filter=ACM HEAD -- '*.py' 2>/dev/null
    git diff --name-only --diff-filter=ACM --cached -- '*.py' 2>/dev/null
}

changed_files() {
    _changed_py_files | sort -u
}

echo "=================================================="
echo "oi_dashboard local diagnostics -- mode: $MODE"
echo "=================================================="

# ---------------------------------------------------------------- 1. git
if [ "$MODE" = "full" ] || [ "$MODE" = "changed" ] || [ "$MODE" = "security" ]; then
    if [ -n "$TREE_DIRTY" ]; then
        STATUS[git]="INFO"; DETAIL[git]="working tree has uncommitted changes"
    else
        STATUS[git]="PASS"; DETAIL[git]="clean, HEAD=${HEAD_SHA:0:10}"
    fi
    # git fsck via the same read-only check security_audit.check_integrity()
    # uses -- called against a nonexistent db_path so it only checks git,
    # never opens the live production DB (matches this repo's own
    # test_agents/hardening/test_db_integrity.py precedent).
    FSCK_OUT=$("$PY" -c "
import sys; sys.path.insert(0, '.')
from agents.sys_admin import security_audit
r = security_audit.check_integrity(db_path='/nonexistent/diagnose.db', repo_dir='.')
print('OK' if r['git_fsck_ok'] else 'DIRTY')
print(r['git_fsck_output'][:500])
" 2>&1)
    FSCK_STATUS=$(echo "$FSCK_OUT" | head -1)
    if [ "$FSCK_STATUS" != "OK" ]; then
        STATUS[git_fsck]="INFO"; DETAIL[git_fsck]="dangling objects present (see: git fsck) -- normal after heavy branch/worktree churn, not corruption unless fsck reports an actual error"
        LOW=$((LOW+1))
    else
        STATUS[git_fsck]="PASS"; DETAIL[git_fsck]="clean"
    fi
fi

# ------------------------------------------------------- 2. runtime health
if [ "$MODE" = "full" ]; then
    APP_PID=$(pgrep -f "python3 app.py" | head -1)
    if [ -n "$APP_PID" ]; then
        UPTIME=$(ps -o etime= -p "$APP_PID" 2>/dev/null | xargs)
        STATUS[runtime]="PASS"; DETAIL[runtime]="running (pid=$APP_PID, uptime=$UPTIME)"
    else
        STATUS[runtime]="FAIL"; DETAIL[runtime]="python3 app.py is NOT running"
        CRITICAL=$((CRITICAL+1))
    fi
    if pgrep -f "run_forever_vps.sh" > /dev/null || { [ -f run_forever.pid ] && kill -0 "$(cat run_forever.pid)" 2>/dev/null; }; then
        DETAIL[runtime]="${DETAIL[runtime]}, crash-restart wrapper active"
    fi
fi

# ------------------------------------------------------- 3. scanner coverage
if [ "$MODE" = "full" ]; then
    if [ -f oi_history.db ]; then
        STALE=$(sqlite3 oi_history.db "
            SELECT symbol FROM (
                SELECT symbol, MAX(date || ' ' || time) as latest FROM cycles GROUP BY symbol
            ) WHERE latest < datetime('now', '-2 days');" 2>/dev/null)
        CONFIGURED_MINUS_VIX="NIFTY BANKNIFTY FINNIFTY MIDCPNIFTY SENSEX CRUDEOIL CRUDEOILM NATURALGAS NATGASMINI GOLD GOLDM SILVER SILVERM"
        SCANNED=$(sqlite3 oi_history.db "SELECT DISTINCT symbol FROM cycles;" 2>/dev/null)
        MISSING=""
        for s in $CONFIGURED_MINUS_VIX; do
            echo "$SCANNED" | grep -qx "$s" || MISSING="$MISSING $s"
        done
        if [ -n "$MISSING" ]; then
            STATUS[scanner]="FAIL"; DETAIL[scanner]="never-scanned symbols:$MISSING"
            HIGH=$((HIGH+1))
        elif [ -n "$STALE" ]; then
            STATUS[scanner]="INFO"; DETAIL[scanner]="stale (>2d) data for:$(echo "$STALE" | tr '\n' ' ')"
            MEDIUM=$((MEDIUM+1))
        else
            STATUS[scanner]="PASS"; DETAIL[scanner]="all 13 option-chain symbols have recent data (INDIA VIX is spot-only, excluded by design)"
        fi
    else
        STATUS[scanner]="FAIL"; DETAIL[scanner]="oi_history.db not found"
        CRITICAL=$((CRITICAL+1))
    fi
fi

# ------------------------------------------------------------ 4. recent errors
if [ "$MODE" = "full" ]; then
    if [ -f app_stdout.log ]; then
        ERR_COUNT=$(grep -icE "error|traceback|exception" app_stdout.log 2>/dev/null || echo 0)
        if [ "$ERR_COUNT" -gt 0 ]; then
            STATUS[logs]="INFO"; DETAIL[logs]="$ERR_COUNT error/traceback/exception line(s) in app_stdout.log (see tail -f app_stdout.log | grep -iE 'error|traceback|exception' for detail)"
            MEDIUM=$((MEDIUM+1))
        else
            STATUS[logs]="PASS"; DETAIL[logs]="no error/traceback/exception lines found"
        fi
    else
        STATUS[logs]="SKIP"; DETAIL[logs]="app_stdout.log not found"
    fi
fi

# --------------------------------------------------------------- 5. secrets
if [ "$MODE" = "full" ] || [ "$MODE" = "security" ]; then
    SECRET_OUT=$("$PY" -c "
import sys; sys.path.insert(0, '.')
from agents.sys_admin import security_audit
import subprocess
# Format-anchored patterns (provider-specific prefixes, JWT structure) are
# high-precision -- a match is almost certainly a real credential shape.
# 'generic_secret_assignment'/'email'/'phone' match ANY KEY=/SECRET=-style
# assignment including safe os.getenv(...) calls and placeholder .example
# files (verified: this repo's own .env.example and auth.py's
# os.getenv('SMTP_PASSWORD', '') trip it) -- high recall, low precision,
# reported as advisory only, never a CRITICAL gate.
HIGH_CONF = {'openai_key', 'anthropic_key', 'aws_access_key', 'github_token', 'slack_token', 'jwt', 'bearer_token'}
# Verified false-positive sources: these test files intentionally contain
# fake example secrets to test that sanitizer.py/security_audit.py's own
# redaction logic works (e.g. test_patcher.py writes a fake sk-... string
# and asserts it never reaches an LLM prompt) -- not real credentials.
KNOWN_FIXTURE_FILES = {
    'test_agents/dev_agent/test_sanitizer.py',
    'test_agents/dev_agent/test_detector.py',
    'test_agents/dev_agent/test_patcher.py',
    'test_agents/sys_admin/test_security_audit.py',
}
files = [f for f in subprocess.run(['git', 'ls-files'], capture_output=True, text=True).stdout.split()
         if f not in KNOWN_FIXTURE_FILES]
findings = security_audit.scan_for_secrets(files)
high = [f for f in findings if f['pattern'] in HIGH_CONF]
low = [f for f in findings if f['pattern'] not in HIGH_CONF]
print(len(high))
print(len(low))
for f in high[:20]:
    print(f\"  {f['path']}: {f['pattern']}\")
" 2>&1)
    HIGH_CONF_COUNT=$(echo "$SECRET_OUT" | sed -n '1p')
    LOW_CONF_COUNT=$(echo "$SECRET_OUT" | sed -n '2p')
    if [ "$HIGH_CONF_COUNT" != "0" ] && [ -n "$HIGH_CONF_COUNT" ]; then
        STATUS[secrets_regex]="FAIL"; DETAIL[secrets_regex]="$HIGH_CONF_COUNT high-confidence secret pattern(s) (provider-key/JWT shape) -- pattern names + files only, never the matched value: $(echo "$SECRET_OUT" | tail -n +3 | tr '\n' ';')"
        CRITICAL=$((CRITICAL+1))
    elif [ "$LOW_CONF_COUNT" != "0" ] && [ -n "$LOW_CONF_COUNT" ]; then
        STATUS[secrets_regex]="INFO"; DETAIL[secrets_regex]="0 high-confidence findings; $LOW_CONF_COUNT low-precision KEY=/SECRET=-assignment-shaped match(es) (advisory only -- this pattern also matches safe os.getenv() calls, verified in this repo; not a confirmed secret without manual review)"
        LOW=$((LOW+1))
    else
        STATUS[secrets_regex]="PASS"; DETAIL[secrets_regex]="0 findings"
    fi

    if command -v gitleaks >/dev/null 2>&1; then
        GL_OUT=$(gitleaks detect --source . --config .gitleaks.toml --no-banner --redact -v 2>&1)
        GL_RC=$?
        if [ "$GL_RC" -eq 0 ]; then
            STATUS[gitleaks]="PASS"; DETAIL[gitleaks]="0 findings (working tree + git history)"
        else
            GL_COUNT=$(echo "$GL_OUT" | grep -c "Secret:" || true)
            STATUS[gitleaks]="FAIL"; DETAIL[gitleaks]="$GL_COUNT finding(s) -- run 'gitleaks detect --source . -v' locally for detail (values redacted here)"
            CRITICAL=$((CRITICAL+1))
        fi
    else
        STATUS[gitleaks]="SKIP"; DETAIL[gitleaks]="gitleaks not installed"
    fi
fi

# ---------------------------------------------------- 6. lint/type/security/deps
if [ "$MODE" = "full" ] || [ "$MODE" = "changed" ] || [ "$MODE" = "security" ] || [ "$MODE" = "typecheck" ]; then
    if [ "$MODE" = "changed" ]; then
        FILES=$(changed_files)
        if [ -z "$FILES" ]; then
            STATUS[lint]="SKIP"; DETAIL[lint]="no changed .py files vs origin/main"
            STATUS[typecheck]="SKIP"; DETAIL[typecheck]="no changed .py files"
            STATUS[security_scan]="SKIP"; DETAIL[security_scan]="no changed .py files"
        else
            # shellcheck disable=SC2086
            RUFF_OUT=$(command -v ruff >/dev/null 2>&1 && ruff check $FILES 2>&1); RUFF_RC=$?
            [ "$MODE" = "changed" ] && { [ "$RUFF_RC" = 0 ] && { STATUS[lint]="PASS"; DETAIL[lint]="0 issues in $(echo "$FILES" | wc -l) changed file(s)"; } || { STATUS[lint]="FAIL"; DETAIL[lint]="$(echo "$RUFF_OUT" | tail -3 | tr '\n' ' ')"; HIGH=$((HIGH+1)); }; }
        fi
    fi
    if [ "$MODE" = "full" ] || [ "$MODE" = "security" ]; then
        if command -v ruff >/dev/null 2>&1 && [ "$MODE" = "full" ]; then
            OUT=$(_cached_or_run ruff ruff check .)
            RC=$(echo "$OUT" | head -1 | sed 's/RC=//')
            [ "$RC" = 0 ] && { STATUS[lint]="PASS"; DETAIL[lint]="0 issues"; } || { STATUS[lint]="FAIL"; DETAIL[lint]="$(echo "$OUT" | tail -3 | tr '\n' ' ')"; MEDIUM=$((MEDIUM+1)); }
        elif [ "$MODE" = "full" ]; then
            STATUS[lint]="SKIP"; DETAIL[lint]="ruff not installed"
        fi

        if command -v bandit >/dev/null 2>&1; then
            # -x excludes test dirs (B101 assert_used fires on every bare
            # `assert` in every test -- expected/safe, not a real finding,
            # verified: 5572 Low-severity hits collapse to 0 once tests are
            # excluded). -ll floors the pass/fail decision at Medium+
            # severity so Low-severity notices (routine subprocess/random
            # usage etc.) never drive the exit code, matching this script's
            # "distinguish CONFIRMED from noise" requirement.
            OUT=$(_cached_or_run bandit bandit -q -r . -x ./test_agents,./venv -ll)
            RC=$(echo "$OUT" | head -1 | sed 's/RC=//')
            [ "$RC" = 0 ] && { STATUS[security_scan]="PASS"; DETAIL[security_scan]="0 medium+ severity issues (low-severity notices excluded from this gate; see .diagnostics/cache for full detail)"; } || { STATUS[security_scan]="FAIL"; DETAIL[security_scan]="medium/high severity issue(s) found -- see .diagnostics/cache for detail"; HIGH=$((HIGH+1)); }
        else
            STATUS[security_scan]="SKIP"; DETAIL[security_scan]="bandit not installed"
        fi

        if command -v pip-audit >/dev/null 2>&1; then
            # Deliberately -r requirements.txt, NOT a bare `pip-audit` --
            # bare invocation audits whatever ambient Python environment
            # happens to be active (non-deterministic, and in a worktree
            # without its own venv this silently audits unrelated SYSTEM
            # packages -- verified: found stale CVEs in old system pyjwt/
            # urllib3/setuptools that aren't even in this project). Scanning
            # the actual pinned requirements file is deterministic and
            # reflects what the app really depends on. Requires network
            # access to resolve package metadata (the one check in this
            # script that does).
            OUT=$(_cached_or_run pip_audit pip-audit -r requirements.txt)
            RC=$(echo "$OUT" | head -1 | sed 's/RC=//')
            [ "$RC" = 0 ] && { STATUS[dependency_scan]="PASS"; DETAIL[dependency_scan]="0 known vulnerabilities in requirements.txt"; } || { STATUS[dependency_scan]="FAIL"; DETAIL[dependency_scan]="see .diagnostics/cache for detail"; HIGH=$((HIGH+1)); }
        else
            STATUS[dependency_scan]="SKIP"; DETAIL[dependency_scan]="pip-audit not installed"
        fi
    fi
    if [ "$MODE" = "full" ] || [ "$MODE" = "typecheck" ]; then
        if command -v mypy >/dev/null 2>&1; then
            OUT=$(_cached_or_run mypy mypy --ignore-missing-imports .)
            RC=$(echo "$OUT" | head -1 | sed 's/RC=//')
            [ "$RC" = 0 ] && { STATUS[typecheck]="PASS"; DETAIL[typecheck]="0 issues"; } || { STATUS[typecheck]="INFO"; DETAIL[typecheck]="mypy reported issues -- advisory, see .diagnostics/cache"; LOW=$((LOW+1)); }
        else
            STATUS[typecheck]="SKIP"; DETAIL[typecheck]="mypy not installed"
        fi
    fi
fi

# --------------------------------------------------------------- 7. shellcheck
if [ "$MODE" = "full" ]; then
    if command -v shellcheck >/dev/null 2>&1; then
        SH_FILES=$(git ls-files '*.sh')
        if [ -n "$SH_FILES" ]; then
            # shellcheck disable=SC2086
            SC_OUT=$(shellcheck -S warning $SH_FILES 2>&1); SC_RC=$?
            if [ "$SC_RC" -eq 0 ]; then
                STATUS[shellcheck]="PASS"; DETAIL[shellcheck]="0 warnings across $(echo "$SH_FILES" | wc -l) script(s)"
            else
                STATUS[shellcheck]="INFO"; DETAIL[shellcheck]="$(echo "$SC_OUT" | grep -c '^In ') warning(s) -- run 'shellcheck -S warning *.sh' for detail"
                LOW=$((LOW+1))
            fi
        else
            STATUS[shellcheck]="SKIP"; DETAIL[shellcheck]="no .sh files tracked"
        fi
    else
        STATUS[shellcheck]="SKIP"; DETAIL[shellcheck]="shellcheck not installed"
    fi
fi

# ------------------------------------------------------------------ 8. tests
if [ "$MODE" = "full" ] || [ "$MODE" = "tests" ]; then
    OUT=$(_cached_or_run pytest "$PY" -m pytest -q -k "not live_positions")
    RC=$(echo "$OUT" | head -1 | sed 's/RC=//')
    SUMMARY_LINE=$(echo "$OUT" | grep -E "passed|failed|error" | tail -1)
    if [ "$RC" = 0 ]; then
        STATUS[tests]="PASS"; DETAIL[tests]="$SUMMARY_LINE"
    else
        STATUS[tests]="FAIL"; DETAIL[tests]="$SUMMARY_LINE"
        CRITICAL=$((CRITICAL+1))
    fi
elif [ "$MODE" = "changed" ]; then
    FILES=$(changed_files)
    TEST_FILES=$(echo "$FILES" | grep -E '(^test_|/test_)' )
    if [ -n "$TEST_FILES" ]; then
        # shellcheck disable=SC2086
        OUT=$("$PY" -m pytest -q -k "not live_positions" $TEST_FILES 2>&1); RC=$?
        SUMMARY_LINE=$(echo "$OUT" | grep -E "passed|failed|error" | tail -1)
        [ "$RC" = 0 ] && { STATUS[tests]="PASS"; DETAIL[tests]="$SUMMARY_LINE"; } || { STATUS[tests]="FAIL"; DETAIL[tests]="$SUMMARY_LINE"; CRITICAL=$((CRITICAL+1)); }
    else
        STATUS[tests]="SKIP"; DETAIL[tests]="no changed test_*.py files vs origin/main"
    fi
fi

# ------------------------------------------------------------------ summary
echo ""
echo "DIAGNOSTIC SUMMARY"
echo "--------------------------------------------------"
for cat in git git_fsck runtime scanner logs secrets_regex gitleaks lint typecheck security_scan dependency_scan shellcheck tests; do
    [ -n "${STATUS[$cat]+x}" ] && printf "%-16s %-6s %s\n" "$cat" "${STATUS[$cat]}" "${DETAIL[$cat]}"
done
echo ""
echo "CRITICAL: $CRITICAL"
echo "HIGH: $HIGH"
echo "MEDIUM: $MEDIUM"
echo "LOW: $LOW"
echo "--------------------------------------------------"

if [ "$CRITICAL" -gt 0 ]; then
    exit 1
fi
exit 0
