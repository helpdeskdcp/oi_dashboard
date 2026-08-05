#!/bin/bash
# verify_system.sh -- run this on the VPS to comprehensively check:
#   1. Are the latest code changes actually deployed (version markers)?
#   2. Is data being saved for ALL 14 symbols?
#   3. Is the process healthy?
#   4. Any obvious problems (DB size, disk space, errors)?
#
# USAGE (on VPS): bash verify_system.sh

cd ~/oi_dashboard || { echo "ERROR: ~/oi_dashboard not found"; exit 1; }

echo "=================================================="
echo "1. FILE VERSION CHECK (are today's changes deployed?)"
echo "=================================================="
declare -A checks=(
    ["app.py"]="institutional_score|PREMIUM_EMA_FAST|BIAS_PERSISTENCE_SECONDS"
    ["oi_engine.py"]="BULLISH ATM SIGNAL|BEARISH ATM SIGNAL"
    ["sr_probability_engine.py"]="classify_price_structure|compute_institutional_entry_score|advance_level_state"
    ["market_structure.py"]="custom_range_levels|calc_adx"
    ["templates/dashboard.html"]="institutional_score|sr-prob-card|meter-needle"
    ["backtest.py"]="simulate_sr_engine_trades"
)
all_ok=true
for file in "${!checks[@]}"; do
    if [ ! -f "$file" ]; then
        echo "  ❌ MISSING: $file"
        all_ok=false
        continue
    fi
    pattern="${checks[$file]}"
    count=$(grep -cE "$pattern" "$file" 2>/dev/null)
    mtime=$(stat -c '%y' "$file" 2>/dev/null | cut -d'.' -f1)
    if [ "$count" -gt 0 ]; then
        echo "  ✅ $file (modified: $mtime, markers found: $count)"
    else
        echo "  ❌ $file -- OUTDATED, markers NOT found (modified: $mtime)"
        all_ok=false
    fi
done

echo ""
echo "=================================================="
echo "2. PROCESS HEALTH"
echo "=================================================="
if pgrep -f "python3 app.py" > /dev/null; then
    pid=$(pgrep -f "python3 app.py" | head -1)
    uptime_info=$(ps -o etime= -p "$pid" 2>/dev/null | xargs)
    echo "  ✅ App is running (PID $pid, uptime: $uptime_info)"
else
    echo "  ❌ App is NOT running!"
    all_ok=false
fi

if pgrep -f "run_forever_vps.sh" > /dev/null; then
    echo "  ✅ Crash-restart wrapper is running"
elif [ -f "run_forever.pid" ] && kill -0 "$(cat run_forever.pid)" 2>/dev/null; then
    echo "  ✅ Crash-restart wrapper is running (confirmed via PID file)"
else
    echo "  ⚠️  Crash-restart wrapper not found (app may not auto-restart if it crashes)"
fi

echo ""
echo "=================================================="
echo "3. DATA SAVING -- per-symbol cycle counts (last 3 days)"
echo "=================================================="
if [ -f "oi_history.db" ]; then
    sqlite3 oi_history.db "
        SELECT symbol,
               COUNT(*) as total_cycles,
               MIN(date) as earliest_date,
               MAX(date) as latest_date,
               MAX(time) as latest_time
        FROM cycles
        WHERE date >= date('now', '-3 days')
        GROUP BY symbol
        ORDER BY symbol;
    " -header -column
    echo ""
    total_rows=$(sqlite3 oi_history.db "SELECT COUNT(*) FROM cycles;")
    db_size=$(du -h oi_history.db | cut -f1)
    echo "  Total historical rows (all-time): $total_rows"
    echo "  Database file size: $db_size"
else
    echo "  ❌ oi_history.db NOT FOUND!"
    all_ok=false
fi

echo ""
echo "=================================================="
echo "4. PAPER TRADING DATA"
echo "=================================================="
if [ -f "oi_history.db" ]; then
    sqlite3 oi_history.db "
        SELECT symbol, status, COUNT(*) as trades
        FROM paper_trades
        GROUP BY symbol, status
        ORDER BY symbol, status;
    " -header -column
fi

echo ""
echo "=================================================="
echo "5. DISK SPACE"
echo "=================================================="
df -h / | tail -1 | awk '{print "  Used: "$3" / "$2" ("$5" full)"}'

echo ""
echo "=================================================="
echo "6. RECENT ERRORS (last 20 in log)"
echo "=================================================="
grep -iE "error|traceback|exception" app_stdout.log | tail -20

echo ""
echo "=================================================="
if [ "$all_ok" = true ]; then
    echo "✅ OVERALL: System looks healthy and up to date."
else
    echo "⚠️  OVERALL: Some issues found above -- review the ❌ items."
fi
echo "=================================================="
