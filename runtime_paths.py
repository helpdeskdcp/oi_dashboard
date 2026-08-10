"""
runtime_paths.py -- canonical, single-source-of-truth paths for this
app's real production topology, so future scripts/tooling never have to
guess or hardcode a filename. This module exists directly because of a
real incident: an earlier deployment prompt assumed "trading_intelligence.db"
(an empty, unused decoy file that happens to exist) instead of the real
"oi_history.db", assumed "logs/app.log" instead of the real
"app_stdout.log" at the repo root (this shared VPS's "logs/" directory
belongs to a completely different application, owned by a different
Linux user), and assumed a generic "pkill -f python.*app.py" pattern
that would have also killed an unrelated service on this same box
(/root/camera_monitor/app.py). See docs/PRODUCTION_RUNTIME.md for the
full topology this module is the code-level source of truth for.

Deliberately at the repo root, NOT under a new "core/" directory -- no
"core/", "backend/", or similar subdirectory exists anywhere in this
project; every standalone, cross-cutting module (oi_engine.py,
intelligence_models.py, mcx_session_config.py, ...) lives at the repo
root, and this file follows that one established convention rather than
starting a second, inconsistent one.

Values here are read at import time and never change during a process's
lifetime -- this is topology, not runtime state (see agents/runtime/
lifecycle.py's get_deployment_status() for the runtime-state facts that
DO change, like uptime/PID).

Real incident this module's own DATABASE_PATH resolution had to be
hardened against, discovered while building it: this shared VPS has a
DB_PATH environment variable already exported in the shell environment
by an UNRELATED project (/root/ai_trading_real), independent of any
.env file. app.py itself is unaffected -- its own DB_PATH constant is
read AFTER load_dotenv(override=True) has already run, and this
project's own .env explicitly sets DB_PATH=oi_history.db, correctly
overriding the leaked shell value. But a standalone script importing
ONLY this module (never app.py -- e.g. manage.py below, which
deliberately avoids importing app.py the same way intelligence_history_
cli.py/intelligence_alerts_cli.py already do, to sidestep app.py's own
Angel One login side effect on import) would otherwise silently resolve
to the WRONG database -- exactly the class of bug this entire module
exists to prevent. Fixed here by loading this project's own .env
explicitly, by absolute path (not python-dotenv's default find_dotenv()
upward search, which has its own separate, already-documented
fragility -- see docs/PRODUCTION_RUNTIME.md), with override=True,
matching app.py's own precedence rule exactly.
"""
import os

from dotenv import load_dotenv

APP_ROOT = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(APP_ROOT, ".env"), override=True)

# Same os.getenv("DB_PATH", "oi_history.db") resolution app.py's own
# DB_PATH constant already uses -- kept in sync deliberately (never
# imports app.py itself; see agents/runtime/lifecycle.py's own
# docstring for why that import is avoided) rather than duplicating a
# different hardcoded value that could drift from the real one.
DATABASE_PATH = os.path.join(APP_ROOT, os.getenv("DB_PATH", "oi_history.db"))

# Same os.getenv("PORT", "5050") resolution app.py's own PORT constant
# already uses.
APP_PORT = int(os.getenv("PORT", "5050"))

# This app's own logs (app_stdout.log, run_forever.log,
# flask_dashboard.log) live at the repo root, NOT in a logs/
# subdirectory -- confirmed during the Milestone 14 deployment incident
# above.
LOG_DIR = APP_ROOT
APP_LOG_FILE = os.path.join(APP_ROOT, "app_stdout.log")
RUN_FOREVER_LOG_FILE = os.path.join(APP_ROOT, "run_forever.log")

# Written by run_forever_vps.sh's own `echo $$ > run_forever.pid` -- this
# is the SUPERVISOR's own PID (the run_forever_vps.sh bash process),
# not app.py's PID. app.py's PID changes on every restart; the
# supervisor's PID stays stable across them, which is exactly why this
# file is the reliable way to confirm the real supervisor -- not just
# some Python process -- owns the current app.py.
PID_FILE = os.path.join(APP_ROOT, "run_forever.pid")

SUPERVISOR_SCRIPT = os.path.join(APP_ROOT, "run_forever_vps.sh")

BACKUP_DIR = os.path.join(APP_ROOT, "backups")
