#!/usr/bin/env bash
# start_all.sh — start the full data stack on spark-22b6
# Usage: ./scripts/start_all.sh [-v|--verbose] spark_nats | spark_influx
#
# Preferred path: starts (or restarts) the manager, which auto_starts all
# components in config order.  Falls back to direct process launch if the
# manager fails to come up.
#
# -v / --verbose  print manager log output and each command run

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

VERBOSE=0
STACK=""
for arg in "$@"; do
    case "$arg" in
        -v|--verbose) VERBOSE=1 ;;
        *) STACK="$arg" ;;
    esac
done

if [[ -z "$STACK" ]]; then
    echo "Usage: $0 [-v] spark_nats | spark_influx"
    exit 1
fi

VENV="$(pwd)/.venv/bin/python"
LOG_DIR="/tmp/ds_logs"
MANAGER_LOG="/tmp/manager_8762.log"
MANAGER_URL="ws://localhost:8762"
mkdir -p "$LOG_DIR"

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; B='\033[0;34m'; N='\033[0m'
ok()   { echo -e "${G}  [ok]${N}   $*"; }
info() { echo -e "${B}  -->  ${N}  $*"; }
warn() { echo -e "${Y}  [warn]${N} $*"; }
fail() { echo -e "${R}  [FAIL]${N} $*"; }
vcmd() { [[ $VERBOSE -eq 1 ]] && echo -e "${Y}  [cmd]${N}  $*" || true; }

kill_proc() {
    local pattern="$1"
    local pids
    pids=$(pgrep -f "$pattern" 2>/dev/null || true)
    if [[ -n "$pids" ]]; then
        warn "killing stale '$pattern' (PID $pids)"
        kill $pids 2>/dev/null || true
        sleep 0.5
        for pid in $pids; do
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null || true
        done
    fi
}

manager_running() {
    ss -tlnp "sport = :8762" 2>/dev/null | grep -q ":8762"
}

wait_port() {
    local port="$1" label="$2" logfile="${3:-}"
    for i in $(seq 1 30); do
        ss -tlnp "sport = :${port}" 2>/dev/null | grep -q ":${port}" && {
            ok "$label on :$port"
            return 0
        }
        sleep 0.5
    done
    fail "$label did not come up on :$port"
    if [[ -n "$logfile" && -f "$logfile" ]]; then
        echo "--- last 20 lines of $logfile ---"
        tail -20 "$logfile"
    fi
    return 1
}

verbose_flag() { [[ $VERBOSE -eq 1 ]] && echo "-v" || echo ""; }

# ── spark_nats ─────────────────────────────────────────────────────────────
if [[ "$STACK" == "spark_nats" ]]; then
    echo -e "\n${B}==> spark_nats  (via manager)${N}"

    # 1. Kill existing manager so we get a clean restart
    if manager_running; then
        info "stopping existing manager"
        vcmd "bash scripts/stop_manager.sh"
        bash scripts/stop_manager.sh
        sleep 1
    fi

    # 2. Kill any stale service processes (manager auto_start will handle them
    #    via port-kill, but this ensures a clean slate for the PID file too)
    info "clearing stale processes"
    kill_proc "stress_runner.py"
    kill_proc "subscriber/api/server.py"
    kill_proc "aws/data_store/server.py"
    kill_proc "source/parquet_writer/writer.py"
    kill_proc "source/nats_bridge/bridge.py"
    kill_proc "nats-server"
    kill_proc "minio server"
    sleep 1

    # 3. Start manager — auto_start:true will launch everything in order
    info "starting manager (auto_start will bring up all services)"
    vcmd ".venv/bin/python manager/manager.py --config manager/config.spark.yaml"
    nohup .venv/bin/python manager/manager.py \
        --config manager/config.spark.yaml \
        >> "$MANAGER_LOG" 2>&1 &
    MANAGER_PID=$!
    echo "         Manager PID $MANAGER_PID  log: $MANAGER_LOG"

    wait_port 8762 "Manager" "$MANAGER_LOG"

    # 4. Stream manager output while services start up
    info "waiting for all services to reach running state..."
    VFLAG=$(verbose_flag)
    if "$VENV" scripts/ws_cmd.py $VFLAG --timeout 60 "$MANAGER_URL" get_status; then
        : # status printed if verbose
    fi

    echo ""
    echo -e "${G}==> spark_nats running${N}  (manager ws: $MANAGER_URL)"
    echo "    Manager log: $MANAGER_LOG"
    if [[ $VERBOSE -eq 1 ]]; then
        echo ""
        echo "    Process table:"
        ps aux | grep -E "minio|nats-server|bridge\.py|writer\.py|aws/data_store/server|subscriber/api/server|stress_runner" \
               | grep -v grep \
               | awk '{printf "    PID %-8s CPU %-5s  %s %s\n", $2, $3"%", $11, $12}' || true
    fi

# ── spark_influx ────────────────────────────────────────────────────────────
elif [[ "$STACK" == "spark_influx" ]]; then
    echo -e "\n${B}==> spark_influx stack${N}"
    for svc in flashmq influxdb telegraf; do
        if systemctl is-active --quiet "$svc"; then
            ok "$svc already running"
        else
            info "starting $svc"
            vcmd "sudo systemctl start $svc"
            sudo systemctl start "$svc"
            ok "$svc started"
        fi
    done
    echo -e "\n${G}==> spark_influx running${N}"

else
    fail "Unknown stack: $STACK"
    echo "Usage: $0 [-v] spark_nats | spark_influx"
    exit 1
fi
