"""
manage.py -- repo-root operational CLI for production maintenance
tasks. Built after a real deployment-prompt incident: an earlier
"AI Developer Prompt" assumed the wrong database filename
("trading_intelligence.db", an empty unused decoy that happens to
exist -- the real one is "oi_history.db"), the wrong log location
("logs/app.log" -- that directory belongs to a completely different
application on this shared VPS, owned by a different Linux user), and
a `pkill -f "python.*app.py"` pattern broad enough to also kill
/root/camera_monitor/app.py, an unrelated service on this same box.
Every command here reads from runtime_paths.py's canonical constants
instead of hardcoding anything, and identifies "this app's own process"
by cwd match, not just a name substring. See docs/PRODUCTION_RUNTIME.md
for the full incident and topology this file is the tooling half of.

Deliberately does NOT import app.py -- matches intelligence_history_
cli.py / intelligence_alerts_cli.py / trading_intelligence_cli.py's own
established reason: importing app.py triggers its own module-level
Angel One SmartAPI login side effect on every invocation, which no
read-only operational command here should ever cause.

Usage:
    python3 manage.py runtime-info
        # read-only: finds the real app.py process by cwd match (never
        # a bare name/cmdline substring, which could match an unrelated
        # app.py elsewhere on a shared VPS), reports its PID/parent
        # PID/cmdline, whether the canonical port is actually listening,
        # the canonical database path, and whether the intelligence
        # snapshot loop looks active (a recent write, not a guess).

    python3 manage.py verify-backup /path/to/backup.db
        # read-only: file exists, SQLite header valid, PRAGMA
        # integrity_check, required tables present, latest snapshot
        # timestamp readable. Deliberately does NOT compare row counts
        # against the live database -- a live, actively-written 300+MB
        # database backed up mid-write will always show a handful of
        # rows written after the backup snapshot was taken; that's
        # normal drift, not corruption, and comparing counts against it
        # produced exactly one false "critical" verdict during the
        # Milestone 14 deployment despite the backup being perfectly
        # valid (confirmed by PRAGMA integrity_check: ok on that same
        # file). This command checks the backup file on its own terms.
"""
import argparse
import datetime as dt
import os
import socket
import sqlite3
import sys

import runtime_paths

REQUIRED_TABLES = ("users", "cycles", "strikes", "intelligence_snapshots_log", "intelligence_alerts_log")
SNAPSHOT_FRESHNESS_THRESHOLD_SECONDS = 300  # 5 minutes -- matches the staggered
                                             # per-symbol background loop's own
                                             # cadence; a longer gap than this
                                             # honestly means "not currently active,"
                                             # not "broken."


def _find_app_process():
    """Scans /proc for the real app.py process -- matched by BOTH cwd
    equal to runtime_paths.APP_ROOT AND a genuine argv element (never a
    cmdline substring) equal to "app.py" or ending in "/app.py". Both
    checks matter: cwd alone isn't enough -- a shell process invoking
    THIS very script from the same directory has a cmdline string that
    happens to CONTAIN the substring "app.py" (e.g. "python3 manage.py
    runtime-info" embedded in a wrapping shell command), which a naive
    substring check would misidentify as the target; splitting cmdline
    on its real NUL-byte argv boundaries and requiring an EXACT
    argument match (not a substring of some other argument) is what
    actually distinguishes "the process whose argv is literally
    ['./venv/bin/python3', 'app.py']" from "a shell running a longer
    command that happens to mention those words." cwd alone also isn't
    enough on its own -- it's what rules out /root/camera_monitor/
    app.py or any other unrelated app.py on this shared VPS. Returns a
    dict, or None if not found."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
            if cwd != runtime_paths.APP_ROOT:
                continue
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                argv = f.read().decode(errors="replace").split("\x00")
            argv = [a for a in argv if a]  # trailing NUL leaves one empty element
            if not any(arg == "app.py" or arg.endswith("/app.py") for arg in argv):
                continue
            cmdline = " ".join(argv)
            with open(f"/proc/{pid}/status") as f:
                ppid = None
                for line in f:
                    if line.startswith("PPid:"):
                        ppid = int(line.split()[1])
                        break
            return {"pid": pid, "ppid": ppid, "cmdline": cmdline}
        except (OSError, ValueError):
            continue  # process exited mid-scan, or a /proc entry we can't read -- skip, don't crash
    return None


def _port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _snapshot_loop_active() -> tuple[bool, str | None]:
    """Read-only: honest freshness check against the real database, not
    a guess. Returns (active, last_snapshot_ts)."""
    if not os.path.exists(runtime_paths.DATABASE_PATH):
        return False, None
    conn = sqlite3.connect(runtime_paths.DATABASE_PATH)
    try:
        row = conn.execute(
            "SELECT ts FROM intelligence_snapshots_log ORDER BY ts DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return False, None  # table doesn't exist yet -- honestly "not active", not a crash
    finally:
        conn.close()
    if row is None:
        return False, None
    last_ts = row[0]
    try:
        age = (dt.datetime.now() - dt.datetime.fromisoformat(last_ts)).total_seconds()
    except ValueError:
        return False, last_ts
    return age <= SNAPSHOT_FRESHNESS_THRESHOLD_SECONDS, last_ts


def _cmd_runtime_info(args) -> int:
    proc = _find_app_process()
    if proc is None:
        print(f"No app.py process found with cwd={runtime_paths.APP_ROOT}", file=sys.stderr)
        print("current pid: n/a", file=sys.stderr)
        return 1

    listening = _port_listening(runtime_paths.APP_PORT)
    active, last_ts = _snapshot_loop_active()

    print(f"current PID: {proc['pid']}")
    print(f"parent PID: {proc['ppid']}")
    print(f"command line: {proc['cmdline']}")
    print(f"listening port {runtime_paths.APP_PORT}: {'yes' if listening else 'no'}")
    print(f"database path: {runtime_paths.DATABASE_PATH}")
    print(f"snapshot loop active: {active}" + (f" (last snapshot: {last_ts})" if last_ts else " (no snapshots logged yet)"))
    return 0


def _cmd_verify_backup(args) -> int:
    path = args.path
    print(f"Verifying: {path}")

    if not os.path.exists(path):
        print("  FAIL: file does not exist")
        return 1
    print("  OK: file exists")

    with open(path, "rb") as f:
        header = f.read(16)
    if header != b"SQLite format 3\x00":
        print("  FAIL: not a valid SQLite database file (bad header)")
        return 1
    print("  OK: valid SQLite header")

    conn = sqlite3.connect(path)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()
        if result[0] != "ok":
            print(f"  FAIL: PRAGMA integrity_check reported: {result[0]}")
            return 1
        print("  OK: PRAGMA integrity_check = ok")

        existing_tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        missing = [t for t in REQUIRED_TABLES if t not in existing_tables]
        if missing:
            print(f"  FAIL: missing required tables: {missing}")
            return 1
        print(f"  OK: all {len(REQUIRED_TABLES)} required tables present")

        try:
            row = conn.execute("SELECT ts FROM intelligence_snapshots_log ORDER BY ts DESC LIMIT 1").fetchone()
            if row:
                print(f"  OK: latest snapshot timestamp readable ({row[0]})")
            else:
                print("  OK: intelligence_snapshots_log readable (no rows yet)")
        except sqlite3.OperationalError as e:
            print(f"  FAIL: could not read latest snapshot timestamp: {e}")
            return 1
    finally:
        conn.close()

    print(f"Backup verification PASSED: {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_info = sub.add_parser("runtime-info", help="read-only: identify and report on the real running app.py process")
    p_info.set_defaults(func=_cmd_runtime_info)

    p_verify = sub.add_parser("verify-backup", help="read-only: verify a backup file's own integrity")
    p_verify.add_argument("path", help="path to the backup .db file")
    p_verify.set_defaults(func=_cmd_verify_backup)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
