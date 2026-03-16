#!/usr/bin/env bash
# stop_manager.sh — stop the NATS stack manager on spark-22b6
# The manager's own stop_all logic will terminate all managed components.

set -euo pipefail

PIDS=$(pgrep -f "manager/manager.py" 2>/dev/null || true)

if [ -z "$PIDS" ]; then
    echo "[INFO] Manager is not running"
    exit 0
fi

echo "[INFO] Stopping manager (PID $PIDS)"
kill $PIDS
sleep 2

if pgrep -f "manager/manager.py" > /dev/null 2>&1; then
    echo "[WARN] Manager still alive — sending SIGKILL"
    pkill -9 -f "manager/manager.py"
fi

echo "[OK]  Manager stopped"
