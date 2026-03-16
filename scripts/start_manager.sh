#!/usr/bin/env bash
# start_manager.sh — start the NATS stack manager on spark-22b6
# Usage: ./scripts/start_manager.sh [config]
#   config defaults to manager/config.spark.yaml

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

CONFIG="${1:-manager/config.spark.yaml}"
LOGFILE="/tmp/manager_8762.log"

if pgrep -f "manager/manager.py" > /dev/null 2>&1; then
    echo "[WARN] Manager is already running (PID $(pgrep -f 'manager/manager.py'))"
    echo "       Run stop_manager.sh first, or it will be replaced."
    exit 1
fi

echo "[INFO] Starting manager with config: $CONFIG"
nohup .venv/bin/python manager/manager.py --config "$CONFIG" >> "$LOGFILE" 2>&1 &
PID=$!
sleep 1

if kill -0 "$PID" 2>/dev/null; then
    echo "[OK]  Manager started (PID $PID)"
    echo "      Log: $LOGFILE"
else
    echo "[FAIL] Manager failed to start — check $LOGFILE"
    tail -20 "$LOGFILE"
    exit 1
fi
