#!/usr/bin/env bash
# start_all.sh — start the full data stack on spark-22b6
# Usage: ./scripts/start_all.sh spark_nats | spark_influx

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

STACK="${1:-}"
if [[ -z "$STACK" ]]; then
    echo "Usage: $0 spark_nats | spark_influx"
    exit 1
fi

VENV=".venv/bin/python"
LOG_DIR="/tmp/ds_logs"
mkdir -p "$LOG_DIR"

kill_port() {
    local port="$1"
    local pids
    pids=$(ss -Htlnp "sport = :${port}" 2>/dev/null | grep -oP 'pid=\K[0-9]+' || true)
    for pid in $pids; do
        echo "  [kill] port $port occupied by PID $pid"
        kill "$pid" 2>/dev/null || true
        sleep 0.3
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
    done
}

kill_proc() {
    local pattern="$1"
    local pids
    pids=$(pgrep -f "$pattern" 2>/dev/null || true)
    for pid in $pids; do
        echo "  [kill] $pattern PID $pid"
        kill "$pid" 2>/dev/null || true
    done
}

wait_port() {
    local port="$1" label="$2"
    for i in $(seq 1 20); do
        ss -tlnp "sport = :${port}" 2>/dev/null | grep -q ":${port}" && \
            { echo "  [ok]   $label on :$port"; return 0; }
        sleep 0.5
    done
    echo "  [WARN] $label did not come up on :$port"
}

# ── spark_nats ─────────────────────────────────────────────────────────────
if [[ "$STACK" == "spark_nats" ]]; then
    echo "==> Starting spark_nats stack"

    echo "--> Killing any existing processes"
    kill_proc "minio server"
    kill_proc "nats-server"
    kill_proc "bridge.py"
    kill_proc "writer.py"
    kill_proc "aws/data_store/server.py"
    kill_proc "subscriber/api/server.py"
    kill_proc "stress_runner.py"
    sleep 1

    echo "--> MinIO"
    nohup aws/data_store/minio server aws/data_store/minio-data \
        --console-address :9011 \
        > "$LOG_DIR/minio.log" 2>&1 &
    wait_port 9000 "MinIO"

    echo "--> NATS"
    nohup nats-server -c manager/nats-server.conf \
        > "$LOG_DIR/nats.log" 2>&1 &
    wait_port 4222 "NATS"

    echo "--> NATS Bridge"
    nohup "$VENV" source/nats_bridge/bridge.py \
        --config source/nats_bridge/config.spark.yaml \
        > "$LOG_DIR/bridge.log" 2>&1 &

    echo "--> Parquet Writer"
    nohup "$VENV" source/parquet_writer/writer.py \
        --config source/parquet_writer/config.yaml \
        > "$LOG_DIR/writer.log" 2>&1 &

    echo "--> AWS Data Store"
    nohup "$VENV" aws/data_store/server.py \
        --config aws/data_store/config.yaml \
        > "$LOG_DIR/aws_server.log" 2>&1 &
    wait_port 8766 "AWS server"

    echo "--> Subscriber API"
    (cd subscriber/api && nohup "$OLDPWD/$VENV" server.py \
        --config config.yaml \
        > "$LOG_DIR/subscriber.log" 2>&1 &)
    wait_port 8767 "Subscriber API"

    echo "--> Stress Runner"
    nohup "$VENV" source/stress_runner/stress_runner.py \
        --config source/stress_runner/config.spark.yaml \
        > "$LOG_DIR/stress_runner.log" 2>&1 &
    wait_port 8769 "Stress Runner"

    echo ""
    echo "==> spark_nats stack running. Logs in $LOG_DIR/"

# ── spark_influx ────────────────────────────────────────────────────────────
elif [[ "$STACK" == "spark_influx" ]]; then
    echo "==> Starting spark_influx stack (systemd services)"

    for svc in flashmq influxdb telegraf; do
        if systemctl is-active --quiet "$svc"; then
            echo "  [ok]   $svc already running"
        else
            echo "  -->    starting $svc"
            sudo systemctl start "$svc"
        fi
    done

    echo ""
    echo "==> spark_influx stack running"

else
    echo "Unknown stack: $STACK"
    echo "Usage: $0 spark_nats | spark_influx"
    exit 1
fi
