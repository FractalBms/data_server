#!/usr/bin/env bash
# stop_all.sh — stop all data stack processes on spark-22b6
# Usage: ./scripts/stop_all.sh [spark_nats | spark_influx | all]
#   Defaults to stopping spark_nats processes.

set -euo pipefail

STACK="${1:-spark_nats}"

kill_proc() {
    local pattern="$1"
    local pids
    pids=$(pgrep -f "$pattern" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        echo "  [stop] $pattern (PID $pids)"
        kill $pids 2>/dev/null || true
    fi
}

if [[ "$STACK" == "spark_nats" || "$STACK" == "all" ]]; then
    echo "==> Stopping spark_nats stack"
    kill_proc "stress_runner.py"
    kill_proc "subscriber/api/server.py"
    kill_proc "aws/data_store/server.py"
    kill_proc "source/parquet_writer/writer.py"
    kill_proc "source/nats_bridge/bridge.py"
    kill_proc "nats-server"
    kill_proc "minio server"
    sleep 1
    echo "==> spark_nats stopped"
fi

if [[ "$STACK" == "spark_influx" || "$STACK" == "all" ]]; then
    echo "==> Stopping spark_influx stack (systemd)"
    for svc in telegraf influxdb flashmq; do
        sudo systemctl stop "$svc" && echo "  [stop] $svc" || true
    done
    echo "==> spark_influx stopped"
fi
