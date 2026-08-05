#!/data/data/com.termux/files/usr/bin/bash
# run_forever.sh -- Termux crash-resilient launcher for the OI dashboard.
# Termux has no systemd, so this is a simple restart-loop instead: if app.py
# ever crashes (uncaught exception, network blip that kills the process, etc.)
# it gets relaunched automatically after a short pause, instead of the
# dashboard just silently staying dead until you notice and restart it by hand.
#
# USAGE:
#   chmod +x run_forever.sh
#   nohup ./run_forever.sh > run_forever.log 2>&1 &
#
# STOP:
#   pkill -f run_forever.sh ; pkill -f "python3 app.py"

cd "$(dirname "$0")"

echo "$(date '+%Y-%m-%d %H:%M:%S') | run_forever.sh started" >> run_forever.log

while true; do
    echo "$(date '+%Y-%m-%d %H:%M:%S') | Starting app.py..." >> run_forever.log
    python3 app.py >> app_stdout.log 2>&1
    EXIT_CODE=$?
    echo "$(date '+%Y-%m-%d %H:%M:%S') | app.py exited with code $EXIT_CODE -- restarting in 10s" >> run_forever.log
    sleep 10
done
