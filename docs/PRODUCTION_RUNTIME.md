# Production Runtime Topology

This document exists because a deployment prompt once assumed the wrong
database filename, the wrong log location, and a process-kill pattern
broad enough to hit an unrelated service on this shared VPS. Every fact
below was verified directly against the live system, not assumed.
`runtime_paths.py` is the code-level source of truth for the paths;
this document is the human-readable one — if they ever disagree,
`runtime_paths.py` is right and this file needs updating.

## Supervisor

This app is **not** run under systemd or gunicorn. It runs as:

```
./venv/bin/python3 app.py
```

supervised by `run_forever_vps.sh` — a bash loop that restarts `app.py`
automatically if it exits, and writes its own PID to `run_forever.pid`
(`echo $$ > run_forever.pid`). The supervisor's PID stays stable across
every `app.py` restart; `app.py`'s own PID changes every time.

To confirm the supervisor genuinely owns the running process:

```bash
cat run_forever.pid                        # supervisor's PID
ps -o ppid= -p <app.py's PID>               # should equal the above
```

Or use the built-in check: `GET /api/runtime/status` → `deployment.supervisor.detected`.

## Real database

```
oi_history.db
```

at the repo root (`runtime_paths.DATABASE_PATH`). **Not**
`trading_intelligence.db` — that file exists in this repo but is an
empty, unused 0-byte decoy. All real data (option chains, paper
trades, intelligence snapshots/alerts, users, everything) lives in
`oi_history.db`.

⚠️ **This shared VPS has a `DB_PATH` environment variable already
exported by an unrelated project** (`/root/ai_trading_real`),
independent of any `.env` file. This app is unaffected in normal
operation because `app.py` calls `load_dotenv(override=True)`, and this
project's own `.env` explicitly sets `DB_PATH=oi_history.db`, which
correctly wins. But **any standalone script that reads `os.getenv("DB_PATH", ...)` without first loading this project's own `.env` will silently resolve to the wrong database.** `runtime_paths.py` handles
this correctly (explicit `load_dotenv(os.path.join(APP_ROOT, ".env"), override=True)` — an absolute path, not python-dotenv's default
upward-searching `find_dotenv()`, which has its own separate fragility,
below). Always `import runtime_paths` rather than reading `DB_PATH`
directly in any new script.

## Real HTTP port

```
5050
```

(`runtime_paths.APP_PORT`, from `PORT` env var, default `5050`). **Not**
port `8000` — that port is bound by `gunicorn`, but it belongs to a
completely different production service on this shared VPS
("Data Care Point Gunicorn" / `datacare.service`), not this app.

## Real log files

At the repo root — `app_stdout.log` (the main app log), `run_forever.log`
(supervisor restart history), `flask_dashboard.log`. **Not** `logs/app.log`
or `logs/gunicorn.log` — a `logs/` directory does exist on this VPS, but
it belongs to a different application entirely (owned by a different
Linux user, unrelated date-partitioned structure). This app never writes
there.

## Other unrelated services on this shared VPS

Discovered while investigating the topology above — worth knowing so a
future broad `pkill`/`ps` pattern doesn't accidentally target one of
these:

| Process | Purpose |
|---|---|
| `/root/camera_monitor/app.py` | Unrelated — **also named `app.py`**, do not `pkill -f "app.py"` |
| `/root/ai_trading_real` (gunicorn, port 5000) | Unrelated trading app, source of the `DB_PATH` env leak above |
| `datacare*.service` (systemd, port 8000) | Unrelated production service |

## Snapshot endpoint format

```
GET /api/intelligence/snapshot?symbol=NIFTY
```

Symbol is a **query parameter**, not a path segment
(`/api/intelligence/snapshot/NIFTY` does not exist — 404). Admin-gated,
same as every route in this app. Response includes `signal_confidence`,
`oi_strength` (deprecated alias, numerically identical), `market_quality`
(`"NO_LIQUIDITY"` | `"THIN"` | `"NORMAL"`).

## Runtime status endpoint

```
GET /api/runtime/status
```

The one canonical status source (Milestone 12's own convention — new
facts get merged into this same endpoint, never a second one). Carries
`scheduler_state`/`control` (Milestone 12) and, since Milestone 14,
`deployment` — app version, git commit, PID, supervisor detection,
uptime, database path, last snapshot timestamp, snapshot write lag.

There is **no** `/health` endpoint in this app.

## Restart procedure

```bash
cd /root/oi_dashboard

# 1. Verify the current commit
git rev-parse --short HEAD

# 2. Backup first (proper SQLite online backup, not `cp` — see rollback below)
python3 -c "
from agents.sys_admin import backup_recovery
backup_id, report = backup_recovery.create_backup(source_db_path='oi_history.db')
print(backup_id, report.severity, report.evidence)
"

# 3. Find the real running PID (never a broad pkill pattern)
python3 manage.py runtime-info

# 4. Restart — target that exact PID only. The supervisor
#    (run_forever_vps.sh) detects the exit and relaunches automatically.
kill -TERM <the PID from step 3>

# 5. Confirm exactly one app.py process afterward
python3 manage.py runtime-info
```

## Verification procedure

```bash
# Process + port + snapshot freshness
python3 manage.py runtime-info

# Startup errors
tail -n 50 app_stdout.log

# Full runtime + deployment status (requires admin login)
curl -s http://127.0.0.1:5050/api/runtime/status   # 302 unauthenticated, expected
```

## Rollback procedure

1. Stop relying on the current code: `git log` to find the previous
   good commit, `git checkout <previous-sha>` (or reset `master`, if
   the bad commit hasn't been built on top of).
2. Restart the same way as above (step 4) — the supervisor picks up
   whatever code is on disk on its next relaunch.
3. If the **database** also needs rolling back (not just code), use
   `agents.sys_admin.backup_recovery.restore_backup(backup_id, dry_run=True)`
   first to preview, then `dry_run=False` to apply. This restores the
   *entire* database to that backup's point in time — only use it if
   something is wrong beyond just the code, since it reverts any real
   data written since the backup too.
4. Verify with the same commands as the verification procedure above.

## Verifying a backup file's own integrity

```bash
python3 manage.py verify-backup /path/to/backup.db
```

Checks the file on its own terms — SQLite header, `PRAGMA
integrity_check`, required tables, latest snapshot timestamp readable.
**Deliberately does not compare row counts against the live database**
— a live, actively-written multi-hundred-MB database backed up mid-write
will always show a handful of rows written after the backup snapshot
was taken; that's normal drift, not corruption, and comparing counts
against it produced exactly one false "critical" verdict during the
Milestone 14 deployment despite the backup being perfectly valid.

## `.env` resolution fragility (test runs only, not production)

Worktree-based test runs can have `python-dotenv`'s default
`find_dotenv()` (used by `app.py`'s bare `load_dotenv(override=True)`
call) walk **upward** from the worktree and find the *live* production
`.env` at `/root/oi_dashboard/.env`, since a worktree never gets its own
copy of an untracked file. Whether this manifests in a given test run
depends on Python's module-import-order (once `agents.config` reads an
env var at import time, the value is cached for the rest of that
process — a later `load_dotenv()` call doesn't retroactively change
it). This does **not** affect the real production process (which
always runs from `/root/oi_dashboard`, where the real `.env` already
is), and `runtime_paths.py` avoids it entirely for its own resolution
by loading `.env` from an explicit absolute path rather than relying on
upward search. Flagged here for awareness, not fixed — changing
`app.py`'s own `load_dotenv()` call is out of scope for this pass.
