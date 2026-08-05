#!/bin/bash
# restart.sh -- lives ON the VPS in ~/oi_dashboard.
#
# USAGE (always run this INTERACTIVELY, after physically SSHing in -- this
# is the pattern that has worked reliably every single time tonight, vs.
# combined single-line `ssh host "cmd1; cmd2 &"` commands run directly from
# Termux, which have repeatedly failed to actually start a new process):
#
#   ssh root@72.62.28.36
#   cd ~/oi_dashboard
#   ./restart.sh
#
# That's it -- one command, run from inside an actual interactive SSH
# session. The script itself handles kill + restart + verification and
# tells you clearly whether it worked.

cd "$(dirname "$0")"

echo "=== Stopping any existing process ==="
pkill -9 -f run_forever_vps.sh 2>/dev/null
pkill -9 -f "python3 app.py" 2>/dev/null
sleep 2

STILL_RUNNING=$(pgrep -f "python3 app.py")
if [ -n "$STILL_RUNNING" ]; then
    echo "WARNING: process(es) still alive after kill -9: $STILL_RUNNING"
    echo "Trying kill again..."
    kill -9 $STILL_RUNNING 2>/dev/null
    sleep 2
fi

echo ""
echo "=== Starting fresh ==="
nohup ./run_forever_vps.sh > /dev/null 2>&1 &
echo "Launched (job: $!), waiting 15s for startup..."
sleep 15

echo ""
echo "=== Verification ==="
NEW_PID=$(pgrep -f "python3 app.py" | head -1)
if [ -n "$NEW_PID" ]; then
    echo "SUCCESS -- new process is running:"
    ps -o pid,etime,cmd -p "$NEW_PID"
    echo ""
    echo "Last few log lines:"
    tail -10 app_stdout.log
else
    echo "FAILED -- no app.py process found after restart attempt."
    echo "Last 30 log lines (check for a startup error):"
    tail -30 app_stdout.log
fi
