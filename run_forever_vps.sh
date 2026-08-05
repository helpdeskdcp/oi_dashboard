#!/bin/bash
# run_forever.sh (VPS / standard Linux version) -- crash-resilient launcher.
#
# FIXED: now uses the venv's own python3 binary directly every time, so
# forgetting `source venv/bin/activate` before running this can never crash
# the app with "ModuleNotFoundError: No module named 'flask'" again.
#
# FIXED (2026-07-18): built-in log rotation -- app_stdout.log and
# run_forever.log used to grow forever with no size cap (unlike the Python
# logger's own RotatingFileHandler, which does cap at 5MB x 5 backups).
# Left unrotated for months, these could genuinely fill the disk. Now
# truncated (with a single .old backup) once they exceed MAX_LOG_SIZE_MB.
# Also writes a PID file so `verify_system.sh` (or anyone) can reliably
# confirm this wrapper loop itself is alive, not just the app.py child.
#
# USAGE:
#   chmod +x run_forever.sh
#   nohup ./run_forever.sh > /dev/null 2>&1 &
#   (no need to source venv/bin/activate first anymore -- this script does it)
#
# STOP:
#   pkill -f run_forever.sh ; pkill -f "python3 app.py"
#
# Once this is confirmed working, consider migrating to the proper systemd
# service (oi-dashboard.service) instead -- VPS has systemd, Termux doesn't,
# so this is the natural next step here (not on Termux).

cd "$(dirname "$0")"

VENV_PYTHON="./venv/bin/python3"
MAX_LOG_SIZE_MB=50

echo $$ > run_forever.pid

rotate_log_if_large() {
    local logfile="$1"
    if [ -f "$logfile" ]; then
        local size_mb=$(du -m "$logfile" 2>/dev/null | cut -f1)
        if [ -n "$size_mb" ] && [ "$size_mb" -ge "$MAX_LOG_SIZE_MB" ]; then
            mv -f "$logfile" "${logfile}.old"
            echo "$(date '+%Y-%m-%d %H:%M:%S') | Rotated $logfile (was ${size_mb}MB)" >> run_forever.log
        fi
    fi
}

echo "$(date '+%Y-%m-%d %H:%M:%S') | run_forever.sh started (PID $$)" >> run_forever.log

if [ ! -x "$VENV_PYTHON" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') | ERROR: $VENV_PYTHON not found or not executable -- did you run 'python3 -m venv venv' and 'pip install -r requirements.txt' in this folder?" >> run_forever.log
    exit 1
fi

while true; do
    rotate_log_if_large "app_stdout.log"
    rotate_log_if_large "run_forever.log"
    echo "$(date '+%Y-%m-%d %H:%M:%S') | Starting app.py (using $VENV_PYTHON)..." >> run_forever.log
    "$VENV_PYTHON" app.py >> app_stdout.log 2>&1
    EXIT_CODE=$?
    echo "$(date '+%Y-%m-%d %H:%M:%S') | app.py exited with code $EXIT_CODE -- restarting in 10s" >> run_forever.log
    sleep 10
done
